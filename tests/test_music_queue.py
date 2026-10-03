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

    def test_skip_vote_majority(self):
        votes = SkipVotes()
        self.assertEqual(votes.vote(1, 4), (1, 2, False))
        self.assertEqual(votes.vote(2, 4), (2, 2, True))
        self.assertEqual(votes.vote(2, 4), (2, 2, True))
        votes.reset()
        self.assertEqual(votes.vote(3, 1), (1, 1, True))


if __name__ == "__main__":
    unittest.main()
