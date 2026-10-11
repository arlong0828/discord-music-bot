import unittest

from music_queue import SkipVotes, Track, TrackQueue


class QueueTests(unittest.TestCase):
    def test_fifo_and_clear(self):
        queue = TrackQueue()
        tracks = [Track("A", "https://example.com/a", 1), Track("B", "https://example.com/b", 2)]
        queue.add(tracks)
        self.assertEqual(queue.pop().title, "A")
        self.assertEqual(queue.clear(), 1)
        self.assertEqual(len(queue), 0)

    def test_add_front_retries_current_track_before_next_track(self):
        queue = TrackQueue()
        current = Track("current", "https://example.com/current", 1)
        queue.add([Track("next", "https://example.com/next", 2)])

        queue.add_front(current)

        self.assertIs(queue.pop(), current)
        self.assertEqual(queue.pop().title, "next")

    def test_skip_vote_majority(self):
        votes = SkipVotes()
        self.assertEqual(votes.vote(1, 4), (1, 2, False))
        self.assertEqual(votes.vote(2, 4), (2, 2, True))
        self.assertEqual(votes.vote(2, 4), (2, 2, True))
        votes.reset()
        self.assertEqual(votes.vote(3, 1), (1, 1, True))


if __name__ == "__main__":
    unittest.main()
