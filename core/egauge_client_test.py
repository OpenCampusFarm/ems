from egauge_client import EGaugeClient

egauge = EGaugeClient()
print(f"line 1 voltage: {egauge.get_l1()}")
print(f"line 2 voltage: {egauge.get_l2()}")
print(f"grid current 1: {egauge.get_s1()}")
print(f"grid current 2: {egauge.get_s2()}")
print(f"ev charger current: {egauge.get_evcharger_current()}")
print(f"cooler current: {egauge.get_cooler_current()}")
print(f"ev charger power: {egauge.get_evcharger_power()}")
print(f"cooler power: {egauge.get_cooler_power()}")
print(f"grid power: {egauge.get_grid_power()}")
print(f"all values: {egauge.get_all_values()}")
