import time
import time

from Loads import openevse as ev

LEVELS = (6, 12, 20, 34, 48)  
SETTLE_S = 5                   


def snapshot(label):
    s = ev._request("GET", "/status").json()
    print(f"{label:>10}: state={s.get('state')}  pilot={s.get('pilot')} A  "
          f"amp={s.get('amp')} mA  power={s.get('power')} W")


status = ev.get_status()
if status is None:
    raise SystemExit("Charger unreachable, check OPENEVSE_URI and network")

cfg = ev._request("GET", "/config").json()
print("max_current_soft:", cfg.get("max_current_soft"),
      " max_current_hard:", cfg.get("max_current_hard"))

try:
    for amps in LEVELS:
        ev.set_charging(True, amps)
        time.sleep(SETTLE_S)
        snapshot(f"{amps} A")

    ev.set_charging(False)
    time.sleep(SETTLE_S)
    snapshot("blocked")
finally:
    ev.release_claim()
    time.sleep(SETTLE_S)
    snapshot("released")
    print("claims:", ev._request("GET", "/claims").json())