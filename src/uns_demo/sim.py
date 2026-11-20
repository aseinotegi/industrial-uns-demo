"""Bridge between the simulator and the UNS, for the demo's own controls.

The simulated plant (``world``) lives on the field network only, as a real plant
would: nothing on the plant network or the internet can reach it. The console still
needs two things that only exist because this is a simulation: to see the scenario
(the pressure profile, where in the cycle we are) and to start a disturbance now.

This bridge sits on both networks and carries exactly that, through the UNS:

* ``planta1/simulacion/escenario`` (retained, every second): the scenario.
* ``planta1/simulacion/perturbacion``: a request to start a disturbance. Only the
  console may publish it (ACL). The answer goes to ``.../perturbacion/resultado``.

Nothing else from the simulator is reachable, and nothing here touches the PLC.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

from . import uns
from .uns import Message

log = logging.getLogger("sim")


def main() -> int:
    from .bus import Bus, setup_logging, stop_on_signal

    setup_logging()
    world = os.environ.get("WORLD_URL", "http://localhost:8080")
    bus = Bus("simulador")

    def on_disturb(topic: str, message: Message) -> None:
        request = urllib.request.Request(f"{world}/escenario/perturbacion", method="POST")
        try:
            with urllib.request.urlopen(request, timeout=5):
                state = "en-marcha"
        except urllib.error.HTTPError as exc:
            state = "ya-hay-una" if exc.code == 409 else "error"
        except OSError:
            state = "error"
        log.info("disturbance requested by %s: %s", message.source, state)
        extra = {"request": message.extra.get("request")}
        bus.publish(f"{uns.SIM_DISTURB}/resultado", Message(state, extra=extra), retain=False)

    bus.subscribe(uns.SIM_DISTURB, on_disturb, retained=False)
    stop = stop_on_signal()
    bus.start()
    while not stop.wait(1):
        try:
            with urllib.request.urlopen(f"{world}/escenario", timeout=3) as response:
                scenario = json.load(response)
        except OSError as exc:
            log.warning("simulator not reachable: %s", exc)
            continue
        phase = scenario.pop("fase_s")
        bus.publish(uns.SIM_SCENARIO, Message(phase, unit="s", extra=scenario), retain=True)
    bus.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
