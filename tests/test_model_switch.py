"""Regression: switching models mid-session must take effect without /restart.

Bug (fixed): Worker.set_model() updated worker.model, but Worker.engine()
returned a cached Engine that captured the model at construction time. All
downstream call sites (system prompt, health_of tool gating, output budget,
stream_chat) re-derive from engine.model — so the stale copy kept steering
every request to the old model until the worker was rebuilt.
"""
from pathlib import Path

from kern import daemon
from kern.journal import create_session


def _worker(tmp_path: Path, model: str = "model-A"):
    sess = create_session(cwd=str(tmp_path), model=model)
    return daemon.Worker(sess, model)


def test_set_model_retargets_live_engine(tmp_path):
    w = _worker(tmp_path)
    eng = w.engine()
    assert eng.model == "model-A"

    w.set_model("model-B")

    # the SAME cached engine must now target the new model — that is the fix.
    eng2 = w.engine()
    assert eng2 is eng, "engine identity should be preserved (no rebuild needed)"
    assert eng2.model == "model-B"


def test_set_model_clears_fenced_fallback(tmp_path):
    w = _worker(tmp_path)
    eng = w.engine()
    eng.forced_fenced = True  # calibrated for model-A's tool health

    w.set_model("model-B")

    assert w.engine().forced_fenced is False, \
        "fenced fallback must reset: it was calibrated for the old model"


def test_set_model_same_model_is_noop(tmp_path):
    w = _worker(tmp_path)
    eng = w.engine()
    eng.forced_fenced = True  # legitimately set for the CURRENT model

    w.set_model("model-A")  # unchanged

    assert w.engine() is eng
    assert w.engine().forced_fenced is True, \
        "re-selecting the same model must not reset fenced state"


def test_set_model_journals_meta(tmp_path):
    w = _worker(tmp_path)
    w.set_model("model-B")
    assert w.session.meta().get("model") == "model-B"


def test_engine_picks_up_direct_model_assignment(tmp_path):
    """Defense in depth: any code path that assigns worker.model directly
    (e.g. the pre-fix resume handshake) still gets a correctly targeted
    engine on the next engine() call."""
    w = _worker(tmp_path)
    w.engine()  # cache an engine on model-A
    w.model = "model-C"

    eng = w.engine()
    assert eng.model == "model-C"
    assert eng.forced_fenced is False


def test_new_engine_before_first_switch_uses_worker_model(tmp_path):
    """No engine cached yet: set_model must not crash and engine() must build
    with the new model."""
    w = _worker(tmp_path)
    w.set_model("model-B")  # before any engine() call
    eng = w.engine()
    assert eng.model == "model-B"
