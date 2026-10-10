import unittest
import types
from datetime import datetime, timezone
from domains.learning.schemas import pronunciation as input_schema
from domains.learning.schemas import pronunciation_report as output_schema
from domains.learning.service import pronunciation_report_service as service
namespace = service.__dict__
FUNCTION = service.build_learning_summary
STAMP = datetime(2026, 10, 9, tzinfo=timezone.utc)

class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.original_io = {key: namespace[key] for key in ("pron_svc", "run_db", "weak_svc")}

    def tearDown(self):
        namespace.update(self.original_io)

    calls = 0
    assertions = 0

    def check(self, actual, expected):
        type(self).assertions += 1
        self.assertEqual(actual, expected)

    def row(self, sid, value=None, text="문장", kind=None, **overrides):
        fields = dict(sentence_id=sid, korean_sentence=text, kind=kind,
                      total_score=value, pronunciation=value, fluency=value, rhythm=value)
        fields.update(overrides)
        return input_schema.PronSentenceScore(**fields)

    def summary(self, rows):
        type(self).calls += 1
        report = input_schema.PronunciationReport(call_id=7, sentences=rows, sounds=[])
        io = []

        async def get_report(**kwargs):
            io.append("report")
            return report

        def get_history(db, member_id):
            io.append("history")
            return []

        class FakeDB:
            def get(self, model, call_id):
                io.append("call_date")
                return types.SimpleNamespace(call_date=STAMP)

        async def run_db(factory, callback):
            io.append("run_db")
            return callback(FakeDB())

        def forbidden(*args, **kwargs):
            raise AssertionError("unexpected external IO")

        # Only application IO is replaced; all adapter helpers and schemas are real.
        namespace["pron_svc"] = types.SimpleNamespace(get_pronunciation_report=get_report,
                                                        get_pronunciation_history=get_history)
        namespace["run_db"] = run_db
        namespace["weak_svc"] = types.SimpleNamespace(get_sound_cards=forbidden)
        coroutine = FUNCTION(3, 7, session_factory=forbidden, client=None, settings=None)
        # Fake awaits finish immediately: execute the actual coroutine without an IO loop.
        try:
            coroutine.send(None)
        except StopIteration as completed:
            result = completed.value
        else:
            coroutine.close()
            raise AssertionError("unexpected coroutine IO suspension")
        self.check(io, ["report", "run_db", "history", "run_db", "call_date"])
        self.check(result.date, STAMP)
        self.check(result.phonemes, [])
        self.check(result.retry_sounds, [])
        self.check(result.sessions, [])
        self.check("completed" in result.model_dump(), False)
        return result

    def test_6_plus_6_threshold(self):
        rows = [self.row(i + 1, 79 if i % 2 == 0 else 80,
                         kind=None if i < 6 else "native") for i in range(12)]
        result = self.summary(rows)
        self.check((result.total, result.passed, len(result.sentences)), (12, 6, 12))
        self.check([s.sentence_id for s in result.sentences], list(range(1, 13)))
        self.check(sum(s.kind == "native" for s in result.sentences), 6)
        self.check(result.overall, 80)

    def test_partial_6_11_12(self):
        for scored in (6, 11, 12):
            with self.subTest(scored=scored):
                result = self.summary([self.row(i + 1, 80 if i < scored else None,
                                          kind=None if i < 6 else "native") for i in range(12)])
                self.check((result.total, result.passed), (12, scored))
                self.check([s.pronunciation for s in result.sentences], [80] * scored + [None] * (12 - scored))
                self.check(result.overall, 80)
                completed_rows = sum(s.pronunciation is not None and 0 <= s.pronunciation <= 100
                                     for s in result.sentences)
                self.check(completed_rows, scored)
                self.check(bool(result.sentences) and completed_rows == len(result.sentences), scored == 12)
                for s in result.sentences[scored:]:
                    self.check([s.total_score, s.pronunciation, s.fluency, s.rhythm], [None] * 4)
                    self.check(s.model_dump()["total_score"], None)

    def test_historical_6(self):
        result = self.summary([self.row(i, 80) for i in range(1, 7)])
        self.check((result.total, result.passed), (6, 6))
        self.check(all("kind" not in s.model_dump() for s in result.sentences), True)

    def test_empty(self):
        result = self.summary([])
        self.check((result.total, result.passed, result.sentences), (0, 0, []))
        self.check([result.overall, result.pronunciation, result.fluency, result.rhythm], [0] * 4)

    def test_blank_duplicate_same_text(self):
        rows = [self.row(1, 100, text=None), self.row(2, 100, text=""),
                self.row(3, 100, text=" \t\n"), self.row(4, 79, text=" 같은 문장 "),
                self.row(4, 100, text="후속 중복"), self.row(5, 80, text=" 같은 문장 "),
                self.row(1, 80, text="유효한 첫 항목")]
        result = self.summary(rows)
        self.check([s.sentence_id for s in result.sentences], [4, 5, 1])
        self.check((result.total, result.passed, result.overall), (3, 2, 80))
        self.check(result.sentences[0].sentence, " 같은 문장 ")
        self.check(result.sentences[0].total_score, 79)

    def test_score_range_all_four_fields(self):
        result = self.summary([self.row(i + 1, x) for i, x in enumerate((0, 79, 80, 100, -1, 101))])
        self.check((result.total, result.passed), (6, 2))
        for field in ("total_score", "pronunciation", "fluency", "rhythm"):
            self.check([getattr(s, field) for s in result.sentences], [0, 79, 80, 100, None, None])
        self.check([result.overall, result.pronunciation, result.fluency, result.rhythm], [65] * 4)

    def test_total_only_pron_only(self):
        result = self.summary([self.row(1, None, total_score=80), self.row(2, None, pronunciation=100)])
        self.check((result.total, result.passed), (2, 1))
        self.check([result.overall, result.pronunciation, result.fluency, result.rhythm], [80, 100, 0, 0])
        self.check([s.pronunciation for s in result.sentences], [None, 100])
        self.check([s.total_score for s in result.sentences], [80, None])

    def test_all_unscored(self):
        result = self.summary([self.row(1), self.row(2, kind="native")])
        self.check((result.total, result.passed), (2, 0))
        self.check([result.overall, result.pronunciation, result.fluency, result.rhythm], [0] * 4)
        self.check(len(result.sentences), 2)
        self.check(sum(s.pronunciation is not None for s in result.sentences), 0)

    def test_all_invalid(self):
        result = self.summary([self.row(1, -1), self.row(2, 101)])
        self.check(result.passed, 0)
        self.check([result.overall, result.pronunciation, result.fluency, result.rhythm], [0] * 4)

    def test_zero_is_scored(self):
        result = self.summary([self.row(1, 0), self.row(2, None)])
        self.check([result.overall, result.pronunciation, result.fluency, result.rhythm], [0] * 4)
        self.check(result.sentences[0].total_score, 0)
        self.check(result.sentences[1].total_score, None)

    def test_python_bankers_round(self):
        for scores, expected in (((80, 81), 80), ((81, 82), 82)):
            with self.subTest(scores=scores):
                result = self.summary([self.row(1, scores[0]), self.row(2, scores[1])])
                self.check([result.overall, result.pronunciation, result.fluency, result.rhythm], [expected] * 4)

    def test_schema_average_int_and_nullable_row(self):
        for field in ("overall", "pronunciation", "fluency", "rhythm"):
            self.check(output_schema.LearningSummaryOut.model_fields[field].annotation, int)
        self.check(output_schema.SentenceScoreOut.model_fields["sentence_id"].annotation, int)
        self.check(output_schema.SentenceScoreOut(sentence_id=1, sentence="문장").model_dump()["total_score"], None)

    def test_independent_metric_means(self):
        result = self.summary([self.row(1, None, total_score=0, pronunciation=80, fluency=-1, rhythm=100),
                                    self.row(2, None, total_score=100, pronunciation=101, fluency=79)])
        self.check([result.overall, result.pronunciation, result.fluency, result.rhythm], [50, 80, 79, 100])
        self.check(result.passed, 1)


