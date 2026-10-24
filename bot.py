#!/usr/bin/env python3
"""Telegram health monitor with fixed T3 service buttons; Python standard library only."""
import argparse
import datetime
import getpass
import html
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.request

CONFIG = Path.home() / '.config/macmini-status-bot/config.json'
STATE = Path.home() / '.local/state/macmini-status-bot/state.json'
BUTTON = {'inline_keyboard': [[{'text': 'Status Check', 'callback_data': 'status'}],
                              [{'text': 'Refresh providers', 'callback_data': 'refresh'},
                               {'text': 'Restart T3', 'callback_data': 'restart'}]]}
BUTTON_DATA = re.compile(r'(status|refresh|restart|cancel|confirm:[0-9a-f]{8})')
# T3 Code runs as a user service. The provider refresh helper writes its result here.
T3_UNIT = 't3code.service'
REFRESH_UNIT = 't3-provider-refresh.service'
T3_RUNTIME = Path.home() / '.t3/userdata/server-runtime.json'
T3_STATUS = Path.home() / '.local/state/t3-provider-refresh/status.json'
STATUS_STALE = 2 * 3600
STARTUP_GRACE = 15 * 60
CONFIRM_SECONDS = 60
ACTION_COOLDOWN = 60
FOLLOWUP_SECONDS = 30 * 60


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, path)


def api(token, method, **payload):
    request = urllib.request.Request(
        'https://api.telegram.org/bot' + token + '/' + method,
        data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    # Never log exceptions: urllib errors can contain the credential-bearing URL.
    with urllib.request.urlopen(request, timeout=40) as response:
        result = json.load(response)
    if not result.get('ok'):
        raise RuntimeError('Telegram request failed')
    return result['result']


def authorized(update, cfg):
    """Return the button's data if the configured user pressed a known button, else None."""
    q = update.get('callback_query', {})
    message = q.get('message', {})
    chat = message.get('chat', {})
    data = q.get('data')
    if (isinstance(data, str) and BUTTON_DATA.fullmatch(data)
            and q.get('from', {}).get('id') == cfg['user_id']
            and chat.get('id') == cfg['chat_id']
            and chat.get('type') == 'private'):
        return data
    return None


def command(args):
    try:
        return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def read_number(path):
    try:
        value = float(path.read_text())
        return value if math.isfinite(value) else None
    except (OSError, ValueError):
        return None


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def seconds_since(timestamp):
    try:
        then = datetime.datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        return (datetime.datetime.now(datetime.timezone.utc) - then).total_seconds()
    except (AttributeError, TypeError, ValueError):
        return None


def server_answers(origin):
    # Only the local server; the runtime file must not redirect this request.
    if not isinstance(origin, str) or not origin.startswith('http://127.0.0.1:'):
        return False
    try:
        with urllib.request.urlopen(origin + '/api/auth/session', timeout=3):
            return True
    except urllib.error.HTTPError:
        return True
    except (OSError, ValueError):
        return False


def tunnel_running(cgroup):
    """T3 starts cloudflared in its own service cgroup for remote access."""
    if not cgroup.startswith('/'):
        return None
    try:
        pids = Path('/sys/fs/cgroup' + cgroup, 'cgroup.procs').read_text().split()
    except OSError:
        return None
    for pid in pids:
        try:
            if Path('/proc', pid, 'comm').read_text().strip() == 'cloudflared':
                return True
        except OSError:
            continue
    return False


def t3_sample():
    props = command(['systemctl', '--user', 'show', T3_UNIT, '-p', 'ActiveState',
                     '-p', 'ActiveEnterTimestampMonotonic', '-p', 'NRestarts', '-p', 'ControlGroup'])
    unit = dict(l.split('=', 1) for l in props.splitlines() if '=' in l)
    state = unit.get('ActiveState') or 'unknown'
    runtime = read_json(T3_RUNTIME) or {}
    try:
        uptime = time.monotonic() - int(unit['ActiveEnterTimestampMonotonic']) / 1e6
        restarts = int(unit['NRestarts'])
    except (KeyError, ValueError):
        uptime, restarts = None, None
    active = state == 'active'
    return {'state': state, 'uptime': uptime if active else None, 'restarts': restarts,
            'version': runtime.get('serverVersion'),
            'answers': active and server_answers(runtime.get('origin')),
            'tunnel': tunnel_running(unit.get('ControlGroup', '')) if active else None,
            'check': read_json(T3_STATUS)}


class Sampler:
    def __init__(self):
        self.previous_cpu = None

    @staticmethod
    def cpu_times():
        try:
            cpu = [int(x) for x in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
            return sum(cpu), cpu[3] + cpu[4]
        except (OSError, ValueError, IndexError):
            return None

    def sample(self):
        values = {}
        cores = []
        for hw in Path('/sys/class/hwmon').glob('hwmon*'):
            base = hw if (hw / 'name').exists() else hw / 'device'
            try:
                name = (base / 'name').read_text().strip()
            except OSError:
                continue
            for p in base.glob('temp*_input'):
                value = read_number(p)
                if value is None or not 0 < value / 1000 < 120:
                    continue
                label_path = p.with_name(p.name.replace('_input', '_label'))
                try:
                    label = label_path.read_text().strip()
                except OSError:
                    label = p.name
                if name == 'coretemp':
                    cores.append(value / 1000)
                elif name == 'nouveau':
                    values['GPU'] = value / 1000
                elif label in ('Tp0C', 'TM0P', 'TN1F', 'TN1S'):
                    values[{'Tp0C': 'PSU', 'TM0P': 'Memory'}.get(label, label)] = value / 1000
            if name == 'applesmc':
                values['Fan RPM'] = read_number(base / 'fan1_input')
        if cores:
            values['CPU'] = max(cores)
        # A missing sensor remains unavailable rather than becoming zero.
        try:
            current = self.cpu_times()
            if current and (not self.previous_cpu or current[0] - self.previous_cpu[0] < 50):
                # No usable earlier reading (e.g. first sample): measure briefly instead.
                self.previous_cpu = current
                time.sleep(0.5)
                current = self.cpu_times()
            if self.previous_cpu and current and current[0] > self.previous_cpu[0]:
                (old_total, old_idle), (total, idle) = self.previous_cpu, current
                values['CPU usage %'] = round(100 * (1 - (idle - old_idle) / (total - old_total)), 1)
            self.previous_cpu = current or self.previous_cpu
            mem = {l.split(':')[0]: int(l.split()[1]) for l in Path('/proc/meminfo').read_text().splitlines()}
            values['RAM available GiB'] = mem['MemAvailable'] / 1048576
            values['RAM total GiB'] = mem['MemTotal'] / 1048576
            values['Swap used GiB'] = (mem['SwapTotal'] - mem['SwapFree']) / 1048576
            values['Swap total GiB'] = mem['SwapTotal'] / 1048576
        except (OSError, ValueError, KeyError, IndexError):
            pass
        try:
            fs = os.statvfs('/')
            free, used = fs.f_bavail * fs.f_frsize, (fs.f_blocks - fs.f_bfree) * fs.f_frsize
            values['Disk free GiB'] = free / 1073741824
            values['Disk total GiB'] = fs.f_blocks * fs.f_frsize / 1073741824
            # Same basis as df: space reserved for root is not available to users.
            values['Disk used %'] = 100 * used / (used + free) if used + free else None
        except OSError:
            pass
        props = command(['systemctl', 'show', 'macmini-thermal-guard.service',
                         '-p', 'ActiveState', '-p', 'StatusText'])
        fields = dict(l.split('=', 1) for l in props.splitlines() if '=' in l)
        values['controller'] = fields.get('ActiveState', 'unknown')
        status = fields.get('StatusText', '')
        values['protection'] = ('maximum cooling latched' if 'Fault cooling latched' in status
                                else 'critical shutdown requested' if 'Critical temperature' in status
                                else 'temperature curve' if status.startswith('manual;') else 'unknown')
        limit = re.search(r'CPU speed limit (\d+) MHz', status)
        values['CPU speed limit MHz'] = int(limit.group(1)) if limit else None
        values['t3'] = t3_sample()
        values['time'] = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
        return values


TEMPERATURES = ('CPU', 'GPU', 'PSU', 'Memory', 'TN1F', 'TN1S')
# Early warnings below the thermal guard's sustained shutdown limits (SHUTDOWN).
# The guard limits CPU speed from 80 C and shuts down at 85 C. Without cpufreq
# control it keeps 70 C; this bot cannot see which applies.
WARN = {'CPU': 75, 'GPU': 70, 'PSU': 62, 'Memory': 55, 'TN1F': 70, 'TN1S': 70}
SHUTDOWN = {'CPU': 85, 'GPU': 75, 'PSU': 65, 'Memory': 60, 'TN1F': 75, 'TN1S': 75}
FAN_MAX = 5500
GREEN, YELLOW, RED = '🟢', '🟡', '🔴'
COOLING = {'temperature curve': (GREEN, 'Normal fan curve'),
           'maximum cooling latched': (YELLOW, 'Fan held at maximum (SMC fault flag latched)'),
           'critical shutdown requested': (RED, 'Critical temperature: shutdown requested')}


def faults(sample):
    result = set()
    for key, limit in WARN.items():
        value = sample.get(key)
        if value is None:
            result.add(key + ' reading unavailable')
        elif value >= limit:
            result.add(key + ' approaching configured shutdown limit')
    if sample.get('Fan RPM') is None:
        result.add('Fan reading unavailable')
    if sample.get('controller') != 'active':
        result.add('Thermal controller inactive or unreadable')
    if sample.get('protection') != 'temperature curve':
        result.add('Controller: ' + sample.get('protection', 'unknown'))
    if sample.get('RAM available GiB', 10) < 0.5:
        result.add('Available RAM below 0.5 GiB')
    if sample.get('Disk used %', 0) >= 95:
        result.add('Disk at least 95% full')
    if sample.get('CPU speed limit MHz'):
        result.add('CPU speed limited by thermal guard')
    if 't3' in sample:
        result.update(t3_faults(sample['t3']))
    return sorted(result)


def t3_faults(t3):
    # Keep the text stable: a changed fault list sends an alert.
    result = set()
    if t3['state'] != 'active':
        result.add('T3 server ' + t3['state'])
    elif not t3['answers']:
        result.add('T3 server not answering')
    elif t3['tunnel'] is False:
        result.add('T3 remote tunnel not running')
    check = t3.get('check')
    if not check:
        result.add('T3 provider status unavailable')
        return result
    if check.get('error'):
        result.add('T3 provider check failed')
    age = seconds_since(check.get('finishedAt'))
    if age is None or age > STATUS_STALE:
        result.add('T3 provider status out of date')
    # The startup refresh normally clears provider warnings from a slow boot.
    if t3.get('uptime') is None or t3['uptime'] >= STARTUP_GRACE:
        for provider in check.get('providers', []):
            if provider.get('status') != 'ready':
                result.add(f"{provider.get('name')} provider {provider.get('status')}")
    return result


def duration(seconds):
    minutes = int(seconds // 60)
    if minutes < 60:
        return f'{minutes} min'
    hours = minutes // 60
    return f'{hours} h {minutes % 60} min' if hours < 48 else f'{hours // 24} days'


def local_time(timestamp):
    try:
        then = datetime.datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        return then.astimezone().strftime('%a %H:%M')
    except (AttributeError, TypeError, ValueError):
        return 'unknown time'


def t3_rows(t3):
    """Report rows and short notes for the T3 server and its providers."""
    rows, notes = [], []
    if t3['state'] == 'active':
        text = f"Server {t3.get('version') or '(version unknown)'} running"
        if t3.get('uptime') is not None:
            text += ' for ' + duration(t3['uptime'])
        if t3.get('restarts'):
            text += f", {t3['restarts']} automatic restarts"
        rows.append((GREEN, text))
        if not t3['answers']:
            rows.append((RED, 'Server does not answer local requests'))
            notes.append('T3 not answering')
        level, text = {True: (GREEN, 'Remote tunnel running'),
                       False: (YELLOW, 'Remote tunnel not running')}.get(
            t3['tunnel'], (YELLOW, 'Remote tunnel: no reading'))
        rows.append((level, text))
        if level != GREEN:
            notes.append('T3 remote access may not work')
    else:
        rows.append((RED, 'Server ' + t3['state']))
        notes.append('T3 server ' + t3['state'])
    check = t3.get('check')
    if not check:
        rows.append((YELLOW, 'Providers: no check result yet'))
        notes.append('T3 provider status unknown')
        return rows, notes
    for provider in check.get('providers', []):
        status = provider.get('status')
        text = (f"{provider.get('name')} {provider.get('version') or ''}: {status}, "
                f"{provider.get('auth')}, {provider.get('models')} models")
        if provider.get('message'):
            text += ' — ' + provider['message'][:160]
        level = GREEN if status == 'ready' else YELLOW if status == 'warning' else RED
        rows.append((level, text))
        if level != GREEN:
            notes.append(f"{provider.get('name')} {status}")
    when = local_time(check.get('finishedAt'))
    age = seconds_since(check.get('finishedAt'))
    if check.get('error'):
        rows.append((RED, f"Provider check at {when} failed: {check['error'][:200]}"))
        notes.append('T3 provider check failed')
    elif age is None or age > STATUS_STALE:
        rows.append((YELLOW, f'Last provider check {when} is out of date'))
        notes.append('T3 provider check out of date')
    else:
        rows.append(('⚪', f"Providers checked {when} ({check.get('mode')})"))
    return rows, notes


def num(v):
    return f'{v:.1f}'.rstrip('0').rstrip('.')


def temperature_level(key, v):
    return RED if v >= WARN[key] else YELLOW if v >= WARN[key] - 2 else GREEN


def report(sample, history=(), title='Status check', cleared=False):
    """Telegram HTML message with a one-line verdict and per-reading levels."""
    try:
        when = datetime.datetime.fromisoformat(sample['time']).strftime('%a %d %b, %H:%M')
    except ValueError:
        when = sample['time']
    rows, notes = [], []
    for key in TEMPERATURES:
        v = sample.get(key)
        if v is None:
            rows.append((RED, f'{key}: no reading'))
            notes.append(f'{key} reading missing')
            continue
        level = temperature_level(key, v)
        rows.append((level, f'{key} {num(v)}°C — {num(SHUTDOWN[key] - v)}° below shutdown'))
        if level != GREEN:
            notes.append(f'{key} {"over" if level == RED else "near"} warning level ({WARN[key]}°C)')
    load = []
    ram, total = sample.get('RAM available GiB'), sample.get('RAM total GiB')
    if ram is not None and total:
        level = RED if ram < 0.5 else YELLOW if ram < 1.5 else GREEN
        load.append((level, f'RAM {100 * (1 - ram / total):.0f}% used ({ram:.1f} of {total:.1f} GiB free)'))
        if level != GREEN:
            notes.append('RAM running low')
    else:
        load.append((YELLOW, 'RAM: no reading'))
    disk = sample.get('Disk used %')
    if disk is not None:
        level = RED if disk >= 95 else YELLOW if disk >= 85 else GREEN
        load.append((level, f'Disk {disk:.0f}% used ({sample["Disk free GiB"]:.0f} of '
                            f'{sample["Disk total GiB"]:.0f} GiB free)'))
        if level != GREEN:
            notes.append('disk almost full' if level == RED else 'disk getting full')
    else:
        load.append((YELLOW, 'Disk: no reading'))
    swap, swap_total = sample.get('Swap used GiB'), sample.get('Swap total GiB')
    if swap is not None and swap_total:
        load.append(('⚪', f'Swap {100 * swap / swap_total:.0f}% used ({swap:.1f} of {swap_total:.1f} GiB)'))
    busy = sample.get('CPU usage %')
    load.append(('⚪', f'CPU {busy:.0f}% busy' if busy is not None else 'CPU load: no reading'))
    rpm = sample.get('Fan RPM')
    if rpm is None:
        load.append((RED, 'Fan: no reading'))
        notes.append('fan reading missing')
    else:
        load.append(('⚪', f'Fan {rpm:,.0f} RPM ({min(100, 100 * rpm / FAN_MAX):.0f}% of max)'))
    guard = []
    if sample['controller'] == 'active':
        guard.append((GREEN, 'Thermal guard running'))
    else:
        guard.append((RED, 'Thermal guard ' + sample['controller']))
        notes.append('thermal guard not running')
    level, text = COOLING.get(sample['protection'], (YELLOW, 'Cooling mode ' + sample['protection']))
    guard.append((level, text))
    if level != GREEN:
        notes.append({'maximum cooling latched': 'fan held at max (known SMC flag)'}.get(
            sample['protection'], text.lower()))
    if sample.get('CPU speed limit MHz'):
        guard.append((YELLOW, f"CPU speed limited to {sample['CPU speed limit MHz']} MHz (hot CPU)"))
        notes.append('CPU slowed to cool down')
    services = []
    if 't3' in sample:
        services, service_notes = t3_rows(sample['t3'])
        notes += service_notes
    levels = [lv for lv, _ in rows + load + guard + services]
    overall = RED if RED in levels else YELLOW if YELLOW in levels else GREEN
    verdict = {GREEN: 'All good', YELLOW: 'Watch', RED: 'Needs attention'}[overall]
    def section(heading, items):
        return [f'<b>{heading}</b>'] + [f'{lv} {html.escape(t)}' for lv, t in items]
    lines = [f'{overall} <b>Mac mini: {verdict}</b>']
    if notes:
        summary = '; '.join(notes)
        lines.append(html.escape(summary[0].upper() + summary[1:]))
    lines += [f'<i>{html.escape(title)} · {html.escape(when)}</i>', '']
    lines += section('🌡 Temperatures', rows) + [''] + section('💻 System', load)
    lines += [''] + section('🛡 Protection', guard)
    if services:
        lines += [''] + section('🤖 T3 Code', services)
    if history:
        lines += ['', f'<b>📈 Recent trend</b> ({len(history)} samples, avg / peak)']
        for key, unit in (('CPU', '°C'), ('GPU', '°C'), ('PSU', '°C'), ('CPU usage %', '% busy')):
            nums = [s[key] for s in history if isinstance(s.get(key), (float, int))]
            if nums:
                label = 'CPU load' if key == 'CPU usage %' else key
                lines.append(f'{html.escape(label)}: {num(sum(nums) / len(nums))} / {num(max(nums))}{unit}')
    if cleared and overall == GREEN:
        lines += ['', '✅ Previously reported alerts have cleared.']
    lines += ['', '<i>Wall power not measured.</i>']
    return '\n'.join(lines)


class Controls:
    """State for the action buttons. They start fixed user units; there is no shell."""

    def __init__(self):
        self.confirm = None  # (nonce, monotonic expiry) for a pending restart
        self.last_action = -ACTION_COOLDOWN
        self.followup = None  # (report title, wall start time, monotonic deadline)


def systemctl_user(verb, unit):
    try:
        return subprocess.run(['systemctl', '--user', verb, '--no-block', unit],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def press(data, controls):
    """Handle an action button. Return (toast, message text or None, reply markup)."""
    now = time.monotonic()
    if data == 'cancel':
        controls.confirm = None
        return 'Cancelled', None, None
    if now - controls.last_action < ACTION_COOLDOWN:
        return 'Wait one minute between actions', None, None
    if data == 'restart':
        nonce = secrets.token_hex(4)
        controls.confirm = (nonce, now + CONFIRM_SECONDS)
        return ('Confirm the restart',
                '<b>Restart T3 Code?</b>\nRunning agent turns stop. Threads reconnect after '
                f'the restart. Confirm within {CONFIRM_SECONDS} seconds.',
                {'inline_keyboard': [[{'text': 'Confirm restart', 'callback_data': 'confirm:' + nonce},
                                      {'text': 'Cancel', 'callback_data': 'cancel'}]]})
    if data.startswith('confirm:'):
        pending, controls.confirm = controls.confirm, None
        if not pending or pending[0] != data[len('confirm:'):] or now > pending[1]:
            return 'Confirmation expired. Press Restart T3 again.', None, None
        verb, unit, name, title = 'restart', T3_UNIT, 'T3 restart', 'After T3 restart'
    else:
        verb, unit, name, title = 'start', REFRESH_UNIT, 'Provider refresh', 'After provider refresh'
    controls.last_action = now
    if not systemctl_user(verb, unit):
        return 'Request failed', f'🔴 {name} request failed. Check the user service manager.', BUTTON
    # Restarting T3 also restarts the refresh unit, so both actions end with a provider check.
    controls.followup = (title, time.time(), now + FOLLOWUP_SECONDS)
    return ('Requested', f'⏳ {name} requested. A report follows when the provider check '
            'finishes. It can take several minutes on this machine.', None)


def followup_title(sample, followup):
    """Return the report title when the requested action has a result, else None."""
    title, started, deadline = followup
    check = (sample.get('t3') or {}).get('check') or {}
    age = seconds_since(check.get('finishedAt'))
    if age is not None and time.time() - age >= started - 1:
        return title
    if time.monotonic() > deadline:
        return title + ' (provider check did not finish in 30 min)'
    return None


def load_config():
    if CONFIG.stat().st_mode & 0o077:
        raise ValueError('Configuration must have mode 600')
    cfg = json.loads(CONFIG.read_text())
    if not isinstance(cfg.get('token'), str) or ':' not in cfg['token']:
        raise ValueError('Invalid token')
    if any(type(cfg.get(k)) is not int or cfg[k] <= 0 for k in ('user_id', 'chat_id')):
        raise ValueError('A private chat and numeric user ID are required')
    return cfg


def run():
    cfg = load_config()
    # Refuse a bot already configured for another webhook consumer.
    if api(cfg['token'], 'getWebhookInfo').get('url'):
        raise RuntimeError('Remove the existing webhook before using this dedicated bot')
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        state = {}
    sampler = Sampler()
    controls = Controls()
    history = []
    next_sample = 0
    last_button = -100
    offset = state.get('offset', 0)
    while True:
        try:
            now = time.monotonic()
            if now >= next_sample:
                sample = sampler.sample()
                history.append(sample)
                history = history[-120:]
                next_sample = now + 30
                current_faults = faults(sample)
                changed = current_faults != state.get('faults', [])
                due = time.time() - state.get('sent', 0) >= 3600
                alert_due = changed and time.time() - state.get('alert_sent', 0) >= 300
                if due or alert_due:
                    text = report(sample, history if due else (),
                                  'Hourly summary' if due else 'Alert update',
                                  bool(state.get('faults')) and not current_faults)
                    api(cfg['token'], 'sendMessage', chat_id=cfg['chat_id'], text=text,
                        parse_mode='HTML', reply_markup=BUTTON)
                    state.update(faults=current_faults, alert_sent=time.time())
                    if due:
                        state['sent'] = time.time()
                    atomic_json(STATE, state)
                title = controls.followup and followup_title(sample, controls.followup)
                if title:
                    controls.followup = None
                    api(cfg['token'], 'sendMessage', chat_id=cfg['chat_id'],
                        text=report(sample, title=title), parse_mode='HTML', reply_markup=BUTTON)
            updates = api(cfg['token'], 'getUpdates', offset=offset, timeout=20,
                          allowed_updates=['callback_query'], limit=10)
            for update in updates:
                data = authorized(update, cfg)
                if data == 'status':
                    q = update['callback_query']
                    api(cfg['token'], 'answerCallbackQuery', callback_query_id=q['id'])
                    if time.monotonic() - last_button >= 10:
                        api(cfg['token'], 'sendMessage', chat_id=cfg['chat_id'],
                            text=report(sampler.sample()),
                            parse_mode='HTML', reply_markup=BUTTON)
                        last_button = time.monotonic()
                elif data:
                    q = update['callback_query']
                    toast, text, markup = press(data, controls)
                    api(cfg['token'], 'answerCallbackQuery', callback_query_id=q['id'], text=toast)
                    if data == 'cancel' or data.startswith('confirm:'):
                        # Remove the confirmation buttons, so they cannot be pressed again.
                        api(cfg['token'], 'editMessageReplyMarkup', chat_id=cfg['chat_id'],
                            message_id=q['message'].get('message_id'),
                            reply_markup={'inline_keyboard': []})
                    if text:
                        api(cfg['token'], 'sendMessage', chat_id=cfg['chat_id'], text=text,
                            parse_mode='HTML', **({'reply_markup': markup} if markup else {}))
                offset = max(offset, update['update_id'] + 1)
                state['offset'] = offset
                atomic_json(STATE, state)
        except Exception:
            # No raw update, token, destination, response or exception text in logs.
            print('Monitor operation failed; retrying in 30 seconds.', flush=True)
            time.sleep(30)


def setup():
    token = getpass.getpass('Bot token (hidden): ').strip()
    uid = int(input('Your numeric Telegram user ID: '))
    chat = int(input('Your private chat ID (usually the same): '))
    if uid <= 0 or chat <= 0 or ':' not in token:
        raise ValueError('Invalid private chat configuration')
    atomic_json(CONFIG, {'token': token, 'user_id': uid, 'chat_id': chat})
    print('Private configuration saved. Start the bot in Telegram, then start the service.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['run', 'status', 'setup'])
    action = parser.parse_args().action
    if action == 'status':
        sample = Sampler().sample()
        # Terminal output: drop Telegram HTML markup.
        print(html.unescape(re.sub(r'<[^>]+>', '', report(sample))))
    elif action == 'setup':
        setup()
    else:
        try:
            run()
        except Exception:
            raise SystemExit('Startup failed: check private configuration, connectivity and bot webhook.')
