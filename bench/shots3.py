import asyncio, os, sys, time, traceback

LOG = open("/tmp/shot3_debug.log", "w", buffering=1)
def log(msg):
    LOG.write(f"[{time.monotonic():7.1f}] {msg}\n")
    print(f"[{time.monotonic():7.1f}] {msg}", flush=True)

os.environ.pop("KERN_AUTO_APPROVE", None)
log("imports...")
from kern.tui import KernApp
from kern.tui import PromptArea
log("imports done")

def turn_done(app):
    w = getattr(app, "turn_worker", None)
    return w is not None and w.is_finished

def modal_up(app):
    try:
        return len(getattr(app, "_screen_stack", [])) > 1
    except Exception:
        return False

async def main():
    log("creating app")
    app = KernApp(model="gemini-3.8-flash-api", cwd="/home/marty/kern-playground")
    async with app.run_test(size=(110, 36)) as pilot:
        log("app running")
        inp = app.query_one("#prompt", PromptArea)
        inp.load_text("Edit hello.py to print \"hello v2\" instead, then run it.")
        await pilot.press("enter")
        log("submitted")
        await pilot.pause(1.0)
        open("/tmp/kern_spinner.svg", "w").write(app.export_screenshot())
        log("spinner shot saved")
        for i in range(300):
            await pilot.pause(0.2)
            if modal_up(app):
                log(f"modal up after {i*0.2:.1f}s")
                break
            if turn_done(app):
                log(f"turn done without modal after {i*0.2:.1f}s")
                break
        open("/tmp/kern_modal.svg", "w").write(app.export_screenshot())
        log("modal shot saved")
        await pilot.press("y")
        log("pressed y")
        for i in range(400):
            await pilot.pause(0.2)
            if modal_up(app):
                await pilot.press("y")
                log("pressed y again")
            if turn_done(app):
                log(f"turn done after {i*0.2:.1f}s")
                break
        log("starting picker worker")
        picker = app.run_worker(app._model_picker(), name="picker-test", exclusive=False)
        for i in range(60):
            await pilot.pause(0.2)
            if modal_up(app):
                log(f"picker modal up after {i*0.2:.1f}s")
                break
        open("/tmp/kern_picker.svg", "w").write(app.export_screenshot())
        log("picker shot saved")
        await pilot.press("escape")
        await pilot.pause(0.5)
        log("escape pressed, picker done")
    log("exiting run_test")
log("main done")
asyncio.run(main())
log("ALL DONE")
