#!/usr/bin/env python3
"""Read-only Telegram health monitor; Python standard library only."""
import argparse
import datetime
import getpass
import json
import math
import os
from pathlib import Path
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
            cpu = [int(x) for x in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
            total, idle = sum(cpu), cpu[3] + cpu[4]
            if self.previous_cpu:
                old_total, old_idle = self.previous_cpu
                if total > old_total:
                    values['CPU usage %'] = round(100 * (1 - (idle - old_idle) / (total - old_total)), 1)
            self.previous_cpu = total, idle
            mem = {l.split(':')[0]: int(l.split()[1]) for l in Path('/proc/meminfo').read_text().splitlines()}
            values['RAM available GiB'] = mem['MemAvailable'] / 1048576
            values['Swap used GiB'] = (mem['SwapTotal'] - mem['SwapFree']) / 1048576
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


def faults(sample):
    result = set()
    for key, limit in {'CPU': 65, 'GPU': 70, 'PSU': 62, 'TN1F': 70, 'TN1S': 70}.items():
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


def report(sample, history=()):
    lines = ['Mac thermal status', sample['time']]
    for key in ('CPU', 'GPU', 'PSU', 'Memory', 'TN1F', 'TN1S'):
        v = sample.get(key)
        lines.append(f'{key}: {v:.1f} C' if v is not None else f'{key}: unavailable')
    for key in ('Fan RPM', 'CPU usage %', 'RAM available GiB', 'Swap used GiB'):
        v = sample.get(key)
        lines.append(f'{key}: {v:.2f}' if v is not None else f'{key}: unavailable')
    lines += ['Controller: ' + sample['controller'], 'Cooling: ' + sample['protection'],
              'Wall power / energy: unavailable (no supported meter)']
    if history:
        lines.append(f'Recent sampled history ({len(history)} samples; not continuous peaks):')
        for key in ('CPU', 'GPU', 'PSU', 'Fan RPM', 'CPU usage %'):
            nums = [s[key] for s in history if isinstance(s.get(key), (float, int))]
            if nums:
                lines.append(f'{key}: average {sum(nums)/len(nums):.1f}, peak {max(nums):.1f}')
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
                    text = report(sample, history if due else ())
                    if current_faults:
                        text += '\nAlerts:\n' + '\n'.join(current_faults)
                    elif state.get('faults'):
                        text += '\nPreviously reported alert conditions cleared.'
                    api(cfg['token'], 'sendMessage', chat_id=cfg['chat_id'], text=text, reply_markup=BUTTON)
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
                            text=report(sampler.sample()), reply_markup=BUTTON)
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
        sampler = Sampler()
        sampler.sample()
        time.sleep(1)
        print(report(sampler.sample()))
    elif action == 'setup':
        setup()
    else:
        try:
            run()
        except Exception:
            raise SystemExit('Startup failed: check private configuration, connectivity and bot webhook.')
