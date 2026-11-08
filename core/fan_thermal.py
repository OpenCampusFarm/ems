"""fan_v2 heat balance; R, C and airflow remain uncalibrated assumptions."""
import math
R = 3.0
C = 1.0
SENSIBLE_KW = 15670 / 3412.142
FAN_KW = .062
H = 1.2 * 1006 / 1000 * (452 * .65) * .00047194745 * 5/9


def advance(temp, outdoor, compressor, fan, dt_hours, capacity=C):
    g = 1/R + (H if fan else 0)
    equilibrium = outdoor - (SENSIBLE_KW if compressor else 0)/g
    return equilibrium + (temp-equilibrium)*math.exp(-g*dt_hours/capacity)


def slopes(temp, outdoor, capacity=C):
    passive = (outdoor-temp)/(R*capacity)
    return passive, passive + H*(outdoor-temp)/capacity, passive-SENSIBLE_KW/capacity
