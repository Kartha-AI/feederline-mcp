# SPDX-License-Identifier: Apache-2.0
"""Fallback sequence impedances (ohm/km) by conductor type when a segment has no r/x."""

CONDUCTORS: dict[str, dict[str, float]] = {
    # overhead ACSR, typical 4-wire multigrounded construction
    "336_acsr": {"r1": 0.1902, "x1": 0.3820, "r0": 0.4664, "x0": 1.1735, "amps": 530},
    "4/0_acsr": {"r1": 0.3679, "x1": 0.4114, "r0": 0.6447, "x0": 1.2342, "amps": 340},
    "1/0_acsr": {"r1": 0.6960, "x1": 0.4370, "r0": 0.9729, "x0": 1.2580, "amps": 230},
    "2_acsr": {"r1": 1.0500, "x1": 0.4540, "r0": 1.3270, "x0": 1.2760, "amps": 180},
    # underground 1/0 AL concentric-neutral
    "1/0_al_cn": {"r1": 0.6120, "x1": 0.1610, "r0": 1.9500, "x0": 0.8950, "amps": 200},
}
DEFAULT = "336_acsr"


def lookup(conductor_type: str | None) -> dict[str, float]:
    key = (conductor_type or "").strip().lower()
    return CONDUCTORS.get(key, CONDUCTORS[DEFAULT])
