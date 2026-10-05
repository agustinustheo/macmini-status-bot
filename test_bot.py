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
        self.assertIn('CPU: unavailable', text)
        self.assertIn('Wall power / energy: unavailable', text)

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

if __name__ == '__main__': unittest.main()
