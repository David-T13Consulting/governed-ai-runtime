"""Lexical retrieval over the indexed chunks. Deterministic and explainable."""
import math
import re
import unicodedata
from collections import Counter

STOP = set(
    "a an the is are was were be been being do does did what which who whom how when "
    "where why to of for in on at by from with without and or not no as it its this "
    "that these those i my me we our you your he she they them if then than so can "
    "could should would must may might will shall about into over under more most "
    "any all each per vs versus".split()
)


def terms(s):
    s = "".join(
        c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c)
    )
    return [w for w in re.findall(r"[a-z0-9]+", s) if w not in STOP and len(w) > 1]


class Index:
    """BM25 over chunks. Small corpus, so this is built per query run."""

    K1 = 1.4
    B = 0.75

    def __init__(self, chunks):
        self.chunks = chunks
        self.tokens = [terms(c.heading + " " + c.text) for c in chunks]
        self.lengths = [len(t) or 1 for t in self.tokens]
        self.avglen = sum(self.lengths) / float(len(self.lengths) or 1)
        self.counts = [Counter(t) for t in self.tokens]
        self.df = Counter()
        for t in self.tokens:
            for w in set(t):
                self.df[w] += 1
        self.n = len(chunks)

    def _idf(self, word):
        df = self.df.get(word, 0)
        if df == 0:
            return 0.0
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def search(self, question, limit=4, min_score=0.5):
        q = terms(question)
        scored = []
        for i, counts in enumerate(self.counts):
            score = 0.0
            for word in q:
                tf = counts.get(word, 0)
                if not tf:
                    continue
                denom = tf + self.K1 * (
                    1 - self.B + self.B * self.lengths[i] / self.avglen
                )
                score += self._idf(word) * (tf * (self.K1 + 1)) / denom
            if score > 0:
                scored.append((score, i))
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        hits = [(s, self.chunks[i]) for s, i in scored[:limit] if s >= min_score]
        return hits
