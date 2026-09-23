"""Offline intent-routing failure checks; never starts a model."""
import unittest
from unittest.mock import Mock, patch

from . import voice_routing as routing


class RoutingTests(unittest.TestCase):
    def client(self, answer='{"route":"chat"}', status='completed'):
        client=Mock()
        client.request.side_effect=[{}, {'thread': {'id': 'router'}}, {}]
        client.events=[
            {'method':'item/completed','params':{'threadId':'router','item':{'type':'agentMessage','text':answer}}},
            {'method':'turn/completed','params':{'threadId':'router','turn':{'status':status}}},
        ]
        return client

    def test_only_valid_completed_chat_classification_enables_fast_path(self):
        for answer,status,expected in [('{"route":"chat"}','completed','chat'),
                ('{"route":"backend"}','completed','backend'), ('not JSON','completed','backend'),
                ('{"route":"chat","extra":true}','completed','backend'),
                ('{"route":"chat"}','failed','backend')]:
            client=self.client(answer,status)
            with self.subTest(answer=answer,status=status), patch.object(routing,'NativeClient',return_value=client):
                self.assertEqual(routing.route_text('.', 'Hello', []),expected)
            client.close.assert_called_once()

    def test_failure_and_no_result_use_backend(self):
        for fail in (True,False):
            client=self.client();client.events=[];client.next.return_value=None
            if fail:client.request.side_effect=RuntimeError('offline')
            with patch.object(routing,'NativeClient',return_value=client):
                self.assertEqual(routing.route_text('.', 'Save this', []),'backend')
            client.close.assert_called_once()

    def test_classifier_has_no_tools_and_receives_bounded_context(self):
        client=self.client()
        with patch.object(routing,'NativeClient',return_value=client):
            routing.route_text('.', 'Yes, do it', [{'kind':'assistant','text':'x'*1000}],
                               {'mcp_servers.household.enabled':False})
        thread=client.request.call_args_list[1].args[1]
        self.assertEqual(thread['dynamicTools'],[])
        self.assertEqual(thread['selectedCapabilityRoots'],[])
        self.assertEqual(thread['config'],{'mcp_servers.household.enabled':False})
        self.assertEqual(thread['approvalPolicy'],'never')
        turn=client.request.call_args_list[2].args[1]
        self.assertLess(len(turn['input'][0]['text']),700)
        self.assertEqual(turn['outputSchema'],routing.SCHEMA)


if __name__ == '__main__': unittest.main()
