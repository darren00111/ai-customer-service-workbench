"""Protocol fixtures based on the published Titan layout, not real shop data."""
import base64
import gzip
import json
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.channels.pdd_wire import receive_envelopes, WireError, LIMIT
import selftest_channel_pilots as fixtures
from selftest_channel_pilots import frame


def number(n):
    out = bytearray()
    while n >= 128:
        out.append((n & 127) | 128); n >>= 7
    return bytes(out + bytes([n]))


def field(tag, value):
    return number(tag * 8 + 2) + number(len(value)) + value


def packet(messages, compressed=False, method=b'titan.notifyDataLite'):
    notification = {'push_type': 2, 'push_data': {'data': [{'chat_type_id': 1, 'message': m} for m in messages]}}
    inner = field(5, b'fixture-seq') + field(6, json.dumps(notification, ensure_ascii=False).encode())
    payload = gzip.compress(inner) if compressed else inner
    body = field(1, method) + field(10, payload)
    return struct.pack('>HHIII', 10, 102, 123, 1, len(body)) + body


class WireTests(unittest.TestCase):
    def decode(self, raw): return receive_envelopes(base64.b64encode(raw).decode(), 2)

    def test_both_plain_and_compressed_notifications(self):
        original = json.loads(frame())['message']
        for compressed in (False, True):
            self.assertEqual(self.decode(packet([original], compressed)), [{'response': 'push', 'message': original}])

    def test_multiple_messages_are_not_dropped(self):
        messages = [json.loads(frame(str(i)))['message'] for i in range(3)]
        self.assertEqual([r['message']['msg_id'] for r in self.decode(packet(messages))], ['0', '1', '2'])

    def test_legacy_json_and_new_json_notifications(self):
        self.assertEqual(receive_envelopes(frame(), 1)[0]['message']['msg_id'], 'm1')
        notification = {'push_type': 2, 'push_data': {'data': [{'chat_type_id': 1, 'message': {'type': 0}}]}}
        self.assertEqual(receive_envelopes(json.dumps(notification), 1)[0]['message']['type'], 0)

    def test_heartbeat_and_browser_control_frames_are_not_chats(self):
        self.assertEqual(self.decode(struct.pack('>HHIII', 0, 0, 0, 0, 0)), [])
        for opcode in (8, 9, 10): self.assertEqual(receive_envelopes('', opcode), [])

    def test_no_guessing_on_protocol_changes_or_truncated_lengths(self):
        raw = packet([])
        for broken in (raw[:-1], raw[:10], b'\x00\x0b' + raw[2:], struct.pack('>HHIII', 10, 102, 0, 0, 2) + b'\x0a\xff'):
            with self.assertRaises(WireError): self.decode(broken)
        with self.assertRaises(WireError): self.decode(packet([], method=b'titan.newChatProtocol'))

    def test_invalid_base64_and_json_are_rejected(self):
        for payload, opcode in [('not base64!', 2), ('[]', 1), ('{', 1)]:
            with self.assertRaises(WireError): receive_envelopes(payload, opcode)

    def test_gzip_limit_applies_before_allocating_unbounded_output(self):
        blob = gzip.compress(b'x' * (LIMIT + 1))
        body = field(1, b'titan.notifyDataLite') + field(10, blob)
        with self.assertRaises(WireError): self.decode(struct.pack('>HHIII', 10, 102, 0, 0, len(body)) + body)

    def test_duplicate_method_or_notify_field_is_rejected(self):
        body = field(1, b'titan.notifyDataLite') * 2 + field(10, b'')
        with self.assertRaises(WireError): self.decode(struct.pack('>HHIII', 10, 102, 0, 0, len(body)) + body)


class BinaryPilotTest(fixtures.PilotTests):
    # Parent scenario suite is executed for the new transport too.
    def ingest(self, **kwargs):
        message = json.loads(frame(**kwargs))['message']
        self.cdp.s.publish({'method':'Network.webSocketFrameReceived','params':{
            'requestId':'socket1','response':{'opcode':2,'payloadData':base64.b64encode(packet([message],True)).decode()}}})
        return self.p.poll()

    def connect(self):
        self.p.inspect(); self.p.bind('100:200', True)
        self.cdp.socket('wss://titan-ws.pinduoduo.com/?access_token=PRIVATE-FIXTURE')


if __name__ == '__main__': unittest.main(verbosity=1)
