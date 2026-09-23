"""Subscription voice boundaries: mocked transport, no account or hosted call."""
import unittest
from unittest.mock import Mock, patch
from . import voice_native as voice


def client(account=None, limits=None, config=None):
    fake = Mock()
    responses = {
        'account/read': {'account': account if account is not None else {'type':'chatgpt','planType':'prolite'}},
        'account/rateLimits/read': {'rateLimitsByLimitId': {'codex': limits if limits is not None else {'primary':{'usedPercent':20}}}},
        'config/read': {'config': config or {}},
        'thread/realtime/listVoices': {'voices': {'v1':['sol','juniper']}},
    }
    fake.request.side_effect = lambda method, params: responses[method]
    return fake


class SubscriptionVoiceTests(unittest.TestCase):
    def test_managed_account_and_included_limits_pass_without_starting_voice(self):
        fake=client(config={'mcp_servers':{'example':{}}})
        result=voice.preflight(fake)
        self.assertEqual(result['authType'],'chatgpt')
        self.assertEqual(result['usageSource'],'included')
        self.assertFalse(result['modelRequestSentByPreflight'])
        self.assertEqual(fake.thread_config,{'mcp_servers.example.enabled':False})
        self.assertFalse(any('start' in c.args[0] for c in fake.request.call_args_list))

    def test_other_authentication_stops_before_configuration_or_model(self):
        for account in ({'type':'apiKey'}, {}, {'type':'chatgpt','planType':'free'}):
            fake=client(account=account)
            with self.subTest(account=account), self.assertRaises(voice.ProbeError):voice.preflight(fake)
            self.assertEqual(fake.request.call_count,1)

    def test_unavailable_or_exhausted_allowance_stops_before_model(self):
        for limits in ({}, {'primary':{'usedPercent':100}},
                       {'primary':{'usedPercent':100},'credits':{'hasCredits':True,'balance':'0'}},
                       {'primary':{'usedPercent':100},'credits':{'hasCredits':True,'balance':'unknown'}},
                       {'primary':{'usedPercent':100},'credits':{'hasCredits':True,'balance':'Infinity'}},
                       {'primary':{'usedPercent':10},'credits':{'hasCredits':True,'balance':'20'},'spendControlReached':True}):
            fake=client(limits=limits)
            with self.subTest(limits=limits), self.assertRaises(voice.ProbeError):voice.preflight(fake)
            self.assertEqual(fake.request.call_count,2)

    def test_purchased_or_unlimited_credits_pass_after_included_limit(self):
        choices = (
            {'hasCredits':True,'unlimited':False,'balance':'2488.6965000000'},
            {'hasCredits':True,'unlimited':True,'balance':None},
        )
        for credits in choices:
            limits={'primary':{'usedPercent':100},'credits':credits,
                    'rateLimitReachedType':'rate_limit_reached'}
            fake=client(limits=limits)
            with self.subTest(credits=credits):
                result=voice.preflight(fake)
            self.assertEqual(result['usageSource'],'credits')
            self.assertEqual(result['credits'],credits)
            self.assertFalse(result['modelRequestSentByPreflight'])

    def test_custom_provider_does_not_silently_replace_native_route(self):
        for config in ({'model_providers':{'openai':{'base_url':'https://example.org'}}},
                       {'chatgpt_base_url':'https://example.org'}):
            with self.subTest(config=config), self.assertRaises(voice.ProbeError):voice.preflight(client(config=config))

    def test_api_key_overrides_are_not_inherited(self):
        overrides={'OPENAI_API_KEY':'test-key','OPENAI_BASE_URL':'test-url','CODEX_API_KEY':'test-key','CODEX_HOME':'test-home'}
        with patch.dict(voice.os.environ,overrides):
            env=voice.launch_environment()
        self.assertTrue(all(key not in env for key in overrides))


if __name__ == '__main__': unittest.main()
