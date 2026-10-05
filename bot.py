#!/usr/bin/env python3
"""Read-only Telegram health monitor; Python standard library only."""
import argparse
import datetime
import getpass
import html
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.request

CONFIG = Path.home() / '.config/macmini-status-bot/config.json'
STATE = Path.home() / '.local/state/macmini-status-bot/state.json'
BUTTON = {'inline_keyboard': [[{'text': 'Status Check', 'callback_data': 'status'}]]}


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
    q = update.get('callback_query', {})
    message = q.get('message', {})
    chat = message.get('chat', {})
    return (q.get('data') == 'status'
            and q.get('from', {}).get('id') == cfg['user_id']
            and chat.get('id') == cfg['chat_id']
            and chat.get('type') == 'private')


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
        props = command(['systemctl', 'show', 'macmini-thermal-guard.service',
                         '-p', 'ActiveState', '-p', 'StatusText'])
        fields = dict(l.split('=', 1) for l in props.splitlines() if '=' in l)
        values['controller'] = fields.get('ActiveState', 'unknown')
        status = fields.get('StatusText', '')
        values['protection'] = ('maximum cooling latched' if 'Fault cooling latched' in status
                                else 'critical shutdown requested' if 'Critical temperature' in status
                                else 'temperature curve' if status.startswith('manual;') else 'unknown')
        values['time'] = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
        return values


TEMPERATURES = ('CPU', 'GPU', 'PSU', 'Memory', 'TN1F', 'TN1S')
# Early warnings below the thermal guard's sustained shutdown limits (SHUTDOWN).
WARN = {'CPU': 65, 'GPU': 70, 'PSU': 62, 'Memory': 55, 'TN1F': 70, 'TN1S': 70}
SHUTDOWN = {'CPU': 70, 'GPU': 75, 'PSU': 65, 'Memory': 60, 'TN1F': 75, 'TN1S': 75}
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
    return sorted(result)


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
    levels = [lv for lv, _ in rows + load + guard]
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
            updates = api(cfg['token'], 'getUpdates', offset=offset, timeout=20,
                          allowed_updates=['callback_query'], limit=10)
            for update in updates:
                if authorized(update, cfg):
                    q = update['callback_query']
                    api(cfg['token'], 'answerCallbackQuery', callback_query_id=q['id'])
                    if time.monotonic() - last_button >= 10:
                        api(cfg['token'], 'sendMessage', chat_id=cfg['chat_id'],
                            text=report(sampler.sample()),
                            parse_mode='HTML', reply_markup=BUTTON)
                        last_button = time.monotonic()
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
