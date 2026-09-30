"""Tests for broadcast.py - the in-process pub/sub Broadcaster (BACKLOG #43)."""
import queue
import unittest

from queryapigate import broadcast


class BroadcasterTests(unittest.TestCase):
    def setUp(self):
        self.b = broadcast.Broadcaster()

    def test_a_fresh_broadcaster_has_no_subscribers(self):
        self.assertEqual(self.b.subscriber_count(), 0)

    def test_subscribe_registers_one_subscriber(self):
        self.b.subscribe()
        self.assertEqual(self.b.subscriber_count(), 1)

    def test_a_published_event_reaches_a_subscriber(self):
        q = self.b.subscribe()
        self.b.publish({'type': 'execution', 'filename': 'q1'}, 'admin')
        self.assertEqual(q.get_nowait(), {'type': 'execution', 'filename': 'q1'})

    def test_every_subscriber_gets_every_event_when_none_are_filtered(self):
        q1, q2 = self.b.subscribe(), self.b.subscribe()
        self.b.publish({'a': 1}, 'admin')
        self.assertEqual(q1.get_nowait(), {'a': 1})
        self.assertEqual(q2.get_nowait(), {'a': 1})

    def test_publishing_with_no_subscribers_does_nothing(self):
        self.b.publish({'a': 1}, 'admin')  # must not raise

    def test_unsubscribe_removes_it(self):
        q = self.b.subscribe()
        self.b.unsubscribe(q)
        self.assertEqual(self.b.subscriber_count(), 0)

    def test_unsubscribing_an_unknown_queue_is_a_no_op(self):
        self.b.unsubscribe(queue.Queue())  # never subscribed - must not raise

    def test_an_unsubscribed_queue_gets_no_further_events(self):
        q = self.b.subscribe()
        self.b.unsubscribe(q)
        self.b.publish({'a': 1}, 'admin')
        self.assertTrue(q.empty())

    def test_a_full_queue_drops_the_event_rather_than_blocking(self):
        q = self.b.subscribe()
        for i in range(broadcast._QUEUE_SIZE):
            self.b.publish({'i': i}, 'admin')
        self.assertEqual(q.qsize(), broadcast._QUEUE_SIZE)
        self.b.publish({'i': 'overflow'}, 'admin')  # must not raise or block
        self.assertEqual(q.qsize(), broadcast._QUEUE_SIZE)  # the overflow event was dropped, not queued
        self.assertEqual(q.get_nowait(), {'i': 0})  # the oldest event is still there - nothing was evicted

    def test_a_filtered_subscriber_only_receives_events_for_its_own_key_name(self):
        mine = self.b.subscribe(key_name='alice')
        others = self.b.subscribe(key_name='bob')
        self.b.publish({'who': 'alice'}, 'alice')
        self.assertEqual(mine.get_nowait(), {'who': 'alice'})
        self.assertTrue(others.empty())

    def test_an_unfiltered_subscriber_still_receives_every_event(self):
        admin = self.b.subscribe(key_name=None)
        self.b.publish({'who': 'alice'}, 'alice')
        self.b.publish({'who': 'bob'}, 'bob')
        self.assertEqual(admin.get_nowait(), {'who': 'alice'})
        self.assertEqual(admin.get_nowait(), {'who': 'bob'})

    def test_two_filtered_subscribers_with_the_same_key_name_both_get_it(self):
        a, b = self.b.subscribe(key_name='alice'), self.b.subscribe(key_name='alice')
        self.b.publish({'who': 'alice'}, 'alice')
        self.assertEqual(a.get_nowait(), {'who': 'alice'})
        self.assertEqual(b.get_nowait(), {'who': 'alice'})


if __name__ == '__main__':
    unittest.main()
