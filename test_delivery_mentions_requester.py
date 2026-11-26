"""Group deliveries @ the person who asked; a requesting bot only gets a reply; private chats and scheduled runs get no @."""
import json
import tempfile
import unittest
from pathlib import Path
from feishu_worker import FeishuAdapter, Inbox


class Adapter(FeishuAdapter):
    def __init__(self, inbox, config):
        self.inbox = inbox
        self.config = config


class RequesterTagTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / 'feishu-apps'
        (root / 'app_0000000000000001').mkdir(parents=True)
        (root / 'app_0000000000000002').mkdir(parents=True)
        (root / 'app_0000000000000002' / 'config.json').write_text(
            json.dumps({'app_id': 'cli_other', 'bot_open_id': 'ou_other_bot'}))
        self.a = Adapter(Inbox(root / 'app_0000000000000001' / 'inbox.sqlite3'),
                         {'app_id': 'cli_me', 'bot_open_id': 'ou_me'})

    def tearDown(self):
        self.tmp.cleanup()

    def tag(self, sender, chat_type='group', route=None, body='结果'):
        return self.a.requester_tag({'chat_type': chat_type, 'sender_id': sender}, route or {}, body)

    def test_person_in_group_is_mentioned(self):
        self.assertEqual(self.tag('ou_person'), '<at user_id="ou_person"></at> ')

    def test_requesting_bot_gets_reply_without_mention(self):
        self.assertEqual(self.tag('cli_other', route={'sender_type': 'app'}), '')
        self.assertEqual(self.tag('cli_unknown', route={'sender_type': 'app'}), '')
        item, route = {'chat_type': 'group', 'sender_id': 'cli_other'}, {'sender_type': 'app'}
        self.assertEqual(self.a.quiet_bot_reply(item, route, '<at user_id="ou_other_bot">动效助手</at> 做好了，<at user_id="ou_person">张三</at>'),
                         '做好了，<at user_id="ou_person">张三</at>')
        self.assertEqual(self.a.quiet_bot_reply({'chat_type': 'group', 'sender_id': 'ou_person'}, {},
                                                '<at user_id="ou_other_bot"></at> 请看'), '<at user_id="ou_other_bot"></at> 请看')

    def test_no_tag_in_private_scheduled_self_or_already_mentioned(self):
        self.assertEqual(self.tag('ou_person', chat_type='p2p'), '')
        self.assertEqual(self.tag('ou_person', route={'scheduled': True}), '')
        self.assertEqual(self.tag('ou_me'), '')
        self.assertEqual(self.tag('ou_person', body='<at user_id="ou_person">张三</at> 好了'), '')


if __name__ == '__main__':
    unittest.main()
