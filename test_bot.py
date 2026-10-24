import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import bot

class Tests(unittest.TestCase):
    def setUp(self):
        self.cfg = {'user_id': 123, 'chat_id': 123}
        self.update = {'callback_query': {'data': 'status', 'from': {'id': 123},
                                         'message': {'chat': {'id': 123, 'type': 'private'}}}}

    def test_only_authorized_private_button(self):
        self.assertTrue(bot.authorized(self.update, self.cfg))
        for change in ('sender', 'chat', 'group', 'command', 'inline'):
            u = copy.deepcopy(self.update)
            q = u['callback_query']
            if change == 'sender': q['from']['id'] = 456
            if change == 'chat': q['message']['chat']['id'] = 456
            if change == 'group': q['message']['chat']['type'] = 'group'
            if change == 'command': q['data'] = 'shutdown'
            if change == 'inline': del q['message']
            self.assertFalse(bot.authorized(u, self.cfg), change)
        self.assertFalse(bot.authorized({'message': {'text': '/status'}}, self.cfg))

    def test_missing_and_hot_sensors_alert(self):
        self.assertIn('CPU reading unavailable', bot.faults({}))
        self.assertIn('PSU approaching configured shutdown limit', bot.faults({'PSU': 62}))

    def test_never_invent_power_or_missing_temperatures(self):
        text = bot.report({'time':'now', 'controller':'unknown', 'protection':'unknown'})
        self.assertIn('🔴 CPU: no reading', text)
        self.assertIn('Needs attention', text)
        self.assertIn('Wall power not measured', text)

    def test_report_verdict_levels_and_escaping(self):
        good = {'time':'2026-10-05T17:14:23+07:00', 'controller':'active',
                'protection':'temperature curve', 'CPU':49.0, 'GPU':55, 'PSU':55,
                'Memory':44, 'TN1F':50, 'TN1S':50, 'Fan RPM':2750.0,
                'RAM available GiB':4.5, 'RAM total GiB':7.5,
                'Swap used GiB':0.5, 'Swap total GiB':2.0, 'CPU usage %':12.0,
                'Disk used %':64.2, 'Disk free GiB':76.4, 'Disk total GiB':219.0}
        text = bot.report(good, [good], 'Hourly summary')
        self.assertTrue(text.startswith('🟢 <b>Mac mini: All good</b>'))
        self.assertIn('Hourly summary · Mon 05 Oct, 17:14', text)
        self.assertIn('🟢 CPU 49°C — 21° below shutdown', text)
        self.assertIn('RAM 40% used (4.5 of 7.5 GiB free)', text)
        self.assertIn('Swap 25% used', text)
        self.assertIn('🟢 Disk 64% used (76 of 219 GiB free)', text)
        self.assertIn('Disk getting full', bot.report(dict(good, **{'Disk used %':88})))
        self.assertIn('🔴 Disk 96% used', bot.report(dict(good, **{'Disk used %':96})))
        self.assertIn('Disk at least 95% full', bot.faults(dict(good, **{'Disk used %':96})))
        self.assertIn('Fan 2,750 RPM (50% of max)', text)
        self.assertIn('Recent trend', text)
        self.assertIn('cleared', bot.report(good, cleared=True))
        watch = dict(good, PSU=60.5, protection='maximum cooling latched')
        text = bot.report(watch)
        self.assertTrue(text.startswith('🟡 <b>Mac mini: Watch</b>'))
        self.assertIn('PSU near warning level (62°C); fan held at max (known SMC flag)', text)
        self.assertIn('🟡 PSU 60.5°C — 4.5° below shutdown', text)
        self.assertNotIn('cleared', bot.report(watch, cleared=True))
        hot = dict(good, GPU=71, controller='inactive', protection='<x>')
        text = bot.report(hot)
        self.assertTrue(text.startswith('🔴 <b>Mac mini: Needs attention</b>'))
        self.assertIn('GPU over warning level', text)
        self.assertIn('Cooling mode &lt;x&gt;', text)
        self.assertIn('Memory approaching configured shutdown limit', bot.faults(dict(good, Memory=55)))

    def test_private_atomic_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            bot.atomic_json(path, {'offset': 42})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(bot.json.loads(path.read_text()), {'offset': 42})

    def test_sensor_read_errors_and_nonfinite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'input'
            self.assertIsNone(bot.read_number(path))
            for value in ['nan', 'inf', 'broken']:
                path.write_text(value)
                self.assertIsNone(bot.read_number(path))

    def test_api_failure_does_not_include_token(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
        with patch('bot.urllib.request.urlopen', return_value=Response()), patch('bot.json.load', return_value={'ok':False}):
            with self.assertRaisesRegex(RuntimeError, '^Telegram request failed$'):
                bot.api('synthetic:credential', 'getUpdates')


def t3(**changes):
    check = {'mode': 'check', 'error': None,
             'finishedAt': bot.datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
             'providers': [{'name': 'Claude', 'version': '2.1.308', 'status': 'ready',
                            'auth': 'authenticated', 'models': 13, 'message': None}]}
    value = {'state': 'active', 'uptime': 3600, 'restarts': 0, 'version': '0.1.1',
             'answers': True, 'tunnel': True, 'check': check}
    value.update(changes)
    return value


class T3Tests(unittest.TestCase):
    def test_healthy_t3_section(self):
        text = bot.report({'time': 'now', 'controller': 'active',
                           'protection': 'temperature curve', 't3': t3()})
        self.assertIn('🤖 T3 Code', text)
        self.assertIn('🟢 Server 0.1.1 running for 1 h 0 min', text)
        self.assertIn('🟢 Claude 2.1.308: ready, authenticated, 13 models', text)
        self.assertEqual(bot.t3_faults(t3()), set())

    def test_server_and_tunnel_faults(self):
        self.assertIn('T3 server failed', bot.t3_faults(t3(state='failed')))
        self.assertIn('T3 server not answering', bot.t3_faults(t3(answers=False)))
        self.assertIn('T3 remote tunnel not running', bot.t3_faults(t3(tunnel=False)))
        text = bot.report({'time': 'now', 'controller': 'active',
                           'protection': 'temperature curve', 't3': t3(state='failed')})
        self.assertIn('🔴 Server failed', text)
        self.assertIn('Needs attention', text)

    def test_provider_warning_waits_for_startup_refresh(self):
        check = t3()['check']
        check['providers'][0].update(status='warning', message='Timed out <25s>')
        self.assertEqual(bot.t3_faults(t3(uptime=120, check=check)), set())
        self.assertIn('Claude provider warning', bot.t3_faults(t3(uptime=3600, check=check)))
        text = bot.report({'time': 'now', 'controller': 'active',
                           'protection': 'temperature curve', 't3': t3(check=check)})
        self.assertIn('🟡 Claude 2.1.308: warning', text)
        self.assertIn('Timed out &lt;25s&gt;', text)

    def test_failed_stale_and_missing_checks(self):
        check = dict(t3()['check'], error='RuntimeError: WebSocket upgrade rejected')
        self.assertIn('T3 provider check failed', bot.t3_faults(t3(check=check)))
        self.assertIn('WebSocket upgrade rejected', bot.report(
            {'time': 'now', 'controller': 'active', 'protection': 'temperature curve',
             't3': t3(check=check)}))
        stale = dict(t3()['check'], finishedAt='2026-01-01T00:00:00+07:00')
        self.assertIn('T3 provider status out of date', bot.t3_faults(t3(check=stale)))
        self.assertIn('T3 provider status unavailable', bot.t3_faults(t3(check=None)))

    def test_buttons_are_allowlisted(self):
        for data in ('status', 'refresh', 'restart', 'cancel', 'confirm:0123abcd'):
            update = {'callback_query': {'data': data, 'from': {'id': 1},
                                         'message': {'chat': {'id': 1, 'type': 'private'}}}}
            self.assertEqual(bot.authorized(update, {'user_id': 1, 'chat_id': 1}), data)
        for data in ('confirm:zz', 'restart now', 'systemctl', None):
            update = {'callback_query': {'data': data, 'from': {'id': 1},
                                         'message': {'chat': {'id': 1, 'type': 'private'}}}}
            self.assertIsNone(bot.authorized(update, {'user_id': 1, 'chat_id': 1}))


class ControlTests(unittest.TestCase):
    def test_restart_needs_matching_confirmation(self):
        controls = bot.Controls()
        with patch('bot.systemctl_user', return_value=True) as systemctl:
            toast, text, markup = bot.press('restart', controls)
            self.assertIn('Restart T3 Code?', text)
            nonce = markup['inline_keyboard'][0][0]['callback_data']
            self.assertIn('expired', bot.press('confirm:00000000', controls)[0])
            systemctl.assert_not_called()
            bot.press('restart', controls)
            nonce = controls.confirm[0]
            toast, text, _ = bot.press('confirm:' + nonce, controls)
            # A used confirmation cannot restart again, even after the cooldown.
            controls.last_action = -bot.ACTION_COOLDOWN
            self.assertIn('expired', bot.press('confirm:' + nonce, controls)[0])
        systemctl.assert_called_once_with('restart', 't3code.service')
        self.assertEqual(toast, 'Requested')
        self.assertEqual(controls.followup[0], 'After T3 restart')

    def test_confirmation_expires(self):
        controls = bot.Controls()
        with patch('bot.systemctl_user', return_value=True) as systemctl:
            bot.press('restart', controls)
            nonce, _ = controls.confirm
            controls.confirm = (nonce, bot.time.monotonic() - 1)
            self.assertIn('expired', bot.press('confirm:' + nonce, controls)[0])
        systemctl.assert_not_called()

    def test_refresh_starts_unit_and_cooldown_applies(self):
        controls = bot.Controls()
        with patch('bot.systemctl_user', return_value=True) as systemctl:
            self.assertEqual(bot.press('refresh', controls)[0], 'Requested')
            self.assertIn('Wait', bot.press('refresh', controls)[0])
            self.assertIn('Wait', bot.press('restart', controls)[0])
        systemctl.assert_called_once_with('start', 't3-provider-refresh.service')

    def test_failed_request_is_reported(self):
        with patch('bot.systemctl_user', return_value=False):
            toast, text, markup = bot.press('refresh', bot.Controls())
        self.assertEqual(toast, 'Request failed')
        self.assertIn('🔴', text)
        self.assertIsNone(bot.Controls().followup)

    def test_followup_waits_for_new_check(self):
        started = bot.time.time()
        followup = ('After provider refresh', started, bot.time.monotonic() + 60)
        old = t3(check=dict(t3()['check'], finishedAt='2026-01-01T00:00:00+07:00'))
        self.assertIsNone(bot.followup_title({'t3': old}, followup))
        self.assertEqual(bot.followup_title({'t3': t3()}, followup), 'After provider refresh')
        late = ('After provider refresh', started, bot.time.monotonic() - 1)
        self.assertIn('did not finish', bot.followup_title({'t3': old}, late))


class LoopTests(unittest.TestCase):
    def test_only_allowed_callback_produces_report_and_offset_survives(self):
        cfg = {'token':'synthetic:credential', 'user_id':123, 'chat_id':123}
        sample = {'time':'now', 'controller':'active', 'protection':'temperature curve',
                  'CPU':40, 'GPU':45, 'PSU':55, 'TN1F':45, 'TN1S':45, 'Fan RPM':2500}
        valid = {'update_id':2, 'callback_query':{'id':'q', 'data':'status',
                 'from':{'id':123}, 'message':{'chat':{'id':123, 'type':'private'}}}}
        calls=[]
        def api(token, method, **kwargs):
            calls.append((method,kwargs))
            if method=='getWebhookInfo':return {}
            if method=='getUpdates':
                if sum(m=='getUpdates' for m,k in calls)>1:raise KeyboardInterrupt()
                return [{'update_id':1,'message':{'text':'shutdown'}},valid]
            return {}
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/'state.json'
            with patch('bot.STATE',state), patch('bot.load_config',return_value=cfg), \
                 patch('bot.Sampler.sample',return_value=sample), patch('bot.api',side_effect=api):
                with self.assertRaises(KeyboardInterrupt):bot.run()
            self.assertEqual(bot.json.loads(state.read_text())['offset'],3)
        self.assertEqual(sum(m=='sendMessage' for m,k in calls),2)
        self.assertTrue(all(k['parse_mode']=='HTML' for m,k in calls if m=='sendMessage'))
        self.assertEqual(sum(m=='answerCallbackQuery' for m,k in calls),1)
        self.assertEqual([k['offset'] for m,k in calls if m=='getUpdates'],[0,3])

    def test_failed_delivery_is_not_recorded_as_sent(self):
        def api(token, method, **kwargs):
            if method=='getWebhookInfo':return {}
            raise OSError('synthetic failure')
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/'state.json'
            with patch('bot.STATE',state), patch('bot.load_config',return_value={'token':'fake','chat_id':123}), \
                 patch('bot.Sampler.sample',return_value={'time':'now','controller':'active','protection':'unknown'}), \
                 patch('bot.api',side_effect=api), patch('bot.time.sleep',side_effect=KeyboardInterrupt), \
                 patch('builtins.print'):
                with self.assertRaises(KeyboardInterrupt):bot.run()
            self.assertFalse(state.exists())

    def test_action_buttons_answer_and_clear_confirmation(self):
        cfg = {'token':'synthetic:credential', 'user_id':123, 'chat_id':123}
        sample = {'time':'now', 'controller':'active', 'protection':'temperature curve'}
        chat = {'chat':{'id':123, 'type':'private'}, 'message_id':7}
        calls=[]
        def api(token, method, **kwargs):
            calls.append((method,kwargs))
            if method=='getWebhookInfo':return {}
            if method=='getUpdates':
                if sum(m=='getUpdates' for m,k in calls)>1:raise KeyboardInterrupt()
                return [{'update_id':1,'callback_query':{'id':'a','data':'refresh','from':{'id':123},'message':chat}},
                        {'update_id':2,'callback_query':{'id':'b','data':'cancel','from':{'id':123},'message':chat}}]
            return {}
        with tempfile.TemporaryDirectory() as tmp:
            with patch('bot.STATE',Path(tmp)/'state.json'), patch('bot.load_config',return_value=cfg), \
                 patch('bot.Sampler.sample',return_value=sample), patch('bot.api',side_effect=api), \
                 patch('bot.systemctl_user',return_value=True) as systemctl:
                with self.assertRaises(KeyboardInterrupt):bot.run()
        systemctl.assert_called_once_with('start','t3-provider-refresh.service')
        answers=[k['text'] for m,k in calls if m=='answerCallbackQuery']
        self.assertEqual(answers,['Requested','Cancelled'])
        edits=[k for m,k in calls if m=='editMessageReplyMarkup']
        self.assertEqual(edits,[{'chat_id':123,'message_id':7,'reply_markup':{'inline_keyboard':[]}}])
        self.assertTrue(any('Provider refresh requested' in k.get('text','') for m,k in calls if m=='sendMessage'))

if __name__ == '__main__': unittest.main()
