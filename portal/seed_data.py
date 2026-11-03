"""Source-data import for the three laboratory inventories supplied with the project.

Every record keeps the exact source text and its source location so staff can audit
what was cleaned. Nothing here invents a room number, a timetable or a condition:

* Room numbers: none of the source files contain one -> stored as NULL and shown as
  "Room not yet provided" until staff enter it.
* Condition: none of the source files record condition -> every item starts as "Unknown".
* Timetable: no timetable was supplied -> no entries are created.

Sources (data/source/):
  instrument DEL Lab.xlsx        -> "DEL Lab"         (column C = item, D = quantity; column E
                                                       repeats D on every row, unlabeled)
  MW_Lab_Data.pdf (scanned)      -> "Microwave Lab"   (title: "List Of Equipments present in
                                                       Microwave Lab"); transcribed to
                                                       MW_Lab_Data_transcribed.csv
  Prof_BP_Das_inventory.csv      -> "Prof. B. P. Das Inventory"
  Solid State Lab eqp_list_prep_sumit.xlsx
                                 -> "Solid State Laboratory" (one record per source row; the
                                    "PLACE OF APPRATUS" column - table T-01..T-13 or RECK (rack) -
                                    is kept in the notes)

Labs are imported by code: on start-up any lab in LABS that is not yet in the database is
added with its equipment, so a new inventory can be added without resetting users/bookings.
"""
import csv
import os
import re

import openpyxl

SOURCE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "source")

LABS = [
    {
        "code": "del-lab",
        "name": "DEL Lab",
        "source_file": "instrument DEL Lab.xlsx",
        "description": "Instrument inventory of the DEL Lab: oscilloscopes, function generators, "
                       "multimeters, power supplies, microprocessor and digital trainer kits, FPGA "
                       "boards and interfacing systems.",
    },
    {
        "code": "microwave-lab",
        "name": "Microwave Lab",
        "source_file": "MW_Lab_Data.pdf",
        "description": "RF and microwave measurement facility: vector network analyzers, RF signal "
                       "sources, spectrum analyzer, DSOs, DC power supplies, a far-field anechoic "
                       "chamber and an LPKF PCB prototyping machine.",
    },
    {
        "code": "prof-bp-das",
        "name": "Prof. B. P. Das Inventory",
        "source_file": "Prof_BP_Das_inventory.csv",
        "description": "Embedded systems and IoT inventory maintained under Prof. B. P. Das: "
                       "Arduino, ESP8266, Raspberry Pi and FPGA boards, PMOD modules, sensors, "
                       "motor drivers and robot kits.",
    },
    {
        "code": "solid-state-lab",
        "name": "Solid State Laboratory",
        "source_file": "Solid State Lab eqp_list_prep_sumit.xlsx",
        "description": "Semiconductor and optoelectronic characterization: Hall effect, four-probe and "
                       "magnetoresistance set-ups, P-N junction and solar-cell experiments, diode and "
                       "He-Ne lasers, source/measure units, LCR meters and spectrometers.",
    },
]

# --- Solid State Laboratory curation ----------------------------------------------
# key = S.N. in the source sheet
# value = (display name, category, manufacturer, model, specification, extra note)
SSL_CURATION = {
    1: ("Diode Laser 532 nm", "Laser", "Holmarc", "", "532 nm", ""),
    2: ("Diode Laser 655 nm", "Laser", "Holmarc", "", "655 nm", ""),
    3: ("Laptop", "Computer", "Asus", "", "", ""),
    4: ("Solar Cell Characterization Apparatus", "Experiment Set-up", "Holmarc", "", "", ""),
    5: ("Vacuum Pump", "Vacuum Pump", "Apex Instruments", "", "", ""),
    6: ("Vacuum Pump", "Vacuum Pump", "", "", "", "Manufacturer not stated in source"),
    7: ("Diode Laser 650 nm", "Laser", "Holmarc", "", "650 nm", ""),
    8: ("Detector Output Measurement Unit", "Optical Measurement", "Holmarc", "", "", ""),
    9: ("P-N Junction Study Set-up", "Experiment Set-up", "SES Instruments", "", "", ""),
    10: ("Digital Gaussmeter", "Gaussmeter", "SES Instruments", "", "", ""),
    11: ("Constant Current Power Supply", "Power Supply", "SES Instruments", "", "Constant current", ""),
    12: ("EMU-50V", "Electromagnet", "SES Instruments", "EMU-50V", "", "Written as 'EMU-50v' in source"),
    13: ("Hall Effect Set-up", "Experiment Set-up", "SES Instruments", "", "", ""),
    14: ("Magnetoresistance Set-up", "Experiment Set-up", "SES Instruments", "", "", ""),
    15: ("Constant Current Power Supply", "Power Supply", "SES Instruments", "", "Constant current", ""),
    16: ("EMU-50V", "Electromagnet", "SES Instruments", "EMU-50V", "", "Written as 'EMU-50v' in source"),
    17: ("Four Probe Set-up", "Experiment Set-up", "SES Instruments", "", "", ""),
    18: ("Desktop Computer (Core i5)", "Computer", "Dell", "", "Intel Core i5", ""),
    19: ("Precision Source/Measure Unit", "Source Measure Unit", "Keysight", "B2902A", "", ""),
    20: ("Four Probe Set-up", "Experiment Set-up", "SES Instruments", "", "", ""),
    21: ("P-N Junction Study Set-up", "Experiment Set-up", "SES Instruments", "", "", ""),
    22: ("Digital Gaussmeter", "Gaussmeter", "SES Instruments", "", "", ""),
    23: ("Constant Current Power Supply", "Power Supply", "SES Instruments", "", "Constant current", ""),
    24: ("EMU-50V", "Electromagnet", "SES Instruments", "EMU-50V", "", "Written as 'EMU-50v' in source"),
    25: ("Hall Effect Set-up", "Experiment Set-up", "SES Instruments", "", "", ""),
    26: ("Digital Gaussmeter", "Gaussmeter", "SES Instruments", "", "", ""),
    27: ("Constant Current Power Supply", "Power Supply", "SES Instruments", "", "Constant current", ""),
    28: ("EMU-50V", "Electromagnet", "SES Instruments", "EMU-50V", "", "Written as 'EMU-50v' in source"),
    29: ("Hall Effect Set-up", "Experiment Set-up", "SES Instruments", "", "", ""),
    30: ("SES-CAMM 2", "Experiment Set-up", "SES Instruments", "CAMM 2", "", "Listed in source only as 'SES-CAMM 2'"),
    31: ("Desktop Computer (Core i5)", "Computer", "Acer", "", "Intel Core i5", ""),
    32: ("Triple Output DC Power Supply", "Power Supply", "Keysight", "E3631A", "Triple output", ""),
    33: ("LCR Meter 20 Hz - 300 kHz", "LCR Meter", "Keysight", "E4980AL", "20 Hz to 300 kHz", ""),
    34: ("Desktop Computer (Core i5)", "Computer", "Spectra", "", "Intel Core i5", "Manufacturer from item name 'Spectra computer'"),
    35: ("Desktop Computer (Core i5)", "Computer", "HP", "", "Intel Core i5", ""),
    36: ("UPS", "UPS / Stabilizer", "iBall", "", "", ""),
    37: ("Spectrophotometer", "Spectrometer", "Holmarc", "SP3480", "", ""),
    38: ("Desktop Computer (Core i5)", "Computer", "Spectra", "", "Intel Core i5", "Manufacturer from item name 'Spectra computer'"),
    39: ("Opto-electronic Source & Measurement Unit", "Optical Measurement", "Holmarc", "", "", "Written as 'opto electronice source & measurment unit' in source"),
    40: ("Diode Laser 655 nm", "Laser", "Holmarc", "", "655 nm", ""),
    41: ("He-Ne Laser with Power Supply & Set-up", "Laser", "REO", "", "Helium-neon", ""),
    42: ("Optical Power Meter", "Optical Measurement", "Holmarc", "", "", ""),
    43: ("Spectrometer", "Spectrometer", "Holmarc", "G600", "", ""),
    44: ("Diode Laser 655 nm", "Laser", "Holmarc", "", "655 nm", ""),
    45: ("Desktop Computer (Core i7)", "Computer", "HP", "", "Intel Core i7", ""),
    46: ("Source Meter", "Source Measure Unit", "Keithley", "2636A", "", ""),
    47: ("Desktop CPU (Core i5, CPU only)", "Computer", "HP", "", "Intel Core i5", "CPU only - no monitor listed"),
    48: ("PXI Chassis", "Data Acquisition", "NI", "PXI-1033", "", "Listed in source as 'NI PXI-1033'"),
    49: ("Temperature Unit", "Temperature Instrument", "Marine", "", "", "Listed in source only as 'Marine temperature'"),
    50: ("Vacuum Pump", "Vacuum Pump", "Value", "", "", ""),
    51: ("Precision Source/Measure Unit", "Source Measure Unit", "Keysight", "B2902A", "", ""),
    52: ("LCR Meter 20 Hz - 300 kHz", "LCR Meter", "Keysight", "E4980AL", "20 Hz to 300 kHz", ""),
    53: ("Desktop Computer (Core i5)", "Computer", "Dell", "", "Intel Core i5", ""),
    54: ("UPS 1000 Plus", "UPS / Stabilizer", "Numeric", "1000 Plus", "", ""),
    55: ("Everbeing Chamber", "Probe Station / Chamber", "Everbeing", "", "", ""),
}

# --- DEL Lab curation -------------------------------------------------------------
# key = first spreadsheet row of the grouped source line
# value = (display name, category, manufacturer, model, specification, extra note)
DEL_CURATION = {
    1: ("Analog Oscilloscope 30 MHz", "Oscilloscope", "Scientific", "HM203", "30 MHz", "Serial nos. listed in source"),
    3: ("Analog Storage Oscilloscope 20 MHz", "Oscilloscope", "Scientific", "HM205", "20 MHz, storage", "Serial nos. listed in source"),
    5: ("Analog Oscilloscope 30 MHz", "Oscilloscope", "Scientific", "SM410", "30 MHz", ""),
    6: ("Analog Oscilloscope 50 MHz", "Oscilloscope", "Scientech", "ST271SD", "50 MHz", ""),
    7: ("Analog Oscilloscope 30 MHz", "Oscilloscope", "Scientific", "SM410", "30 MHz", "Serial nos. listed in source"),
    9: ("Analog Oscilloscope 30 MHz", "Oscilloscope", "Scientific", "", "30 MHz", "Serial nos. listed in source"),
    11: ("Analog Oscilloscope 30 MHz", "Oscilloscope", "Scientific", "SM410", "30 MHz", ""),
    12: ("Digital Storage Oscilloscope", "Oscilloscope", "Keysight", "9030", "50 MHz / 20 MHz (as written in source)", ""),
    14: ("Function Generator", "Function Generator", "Scientific", "HM5030", "", ""),
    15: ("Function Generator 1 MHz", "Function Generator", "Aplab", "2011", "1 MHz", ""),
    16: ("Function Generator 1 MHz", "Function Generator", "Scientific", "8021", "1 MHz", ""),
    17: ("Function Generator 3 MHz", "Function Generator", "Scientific", "5070", "3 MHz", ""),
    18: ("Function Generator 1 MHz", "Function Generator", "Scientech", "", "1 MHz", ""),
    19: ("Function Generator 1 MHz", "Function Generator", "Scientech", "4060", "1 MHz", ""),
    20: ("Function Generator 1 MHz", "Function Generator", "Scientech", "", "1 MHz", ""),
    21: ("Function Generator 3 MHz", "Function Generator", "Scientific", "5030", "3 MHz", ""),
    22: ("Function Generator 3 MHz", "Function Generator", "Scientific", "5070", "3 MHz", ""),
    23: ("Multimeter", "Multimeter", "HIL", "260 (M21506)", "", ""),
    24: ("Digital Multimeter 3½-digit", "Multimeter", "Aplab", "889 / 1004", "3½ digit", ""),
    25: ("Digital Multimeter 4½-digit", "Multimeter", "Aplab", "1015", "4½ digit", ""),
    26: ("Analog Multimeter", "Multimeter", "Kanji", "SDM049", "Analog", ""),
    27: ("Analog Multimeter", "Multimeter", "Kanji", "SDM049", "Analog", ""),
    28: ("Digital Multimeter", "Multimeter", "Scientific", "SM5011A", "", ""),
    29: ("Digital Multimeter", "Multimeter", "Scientific", "SM5011A", "", ""),
    30: ("Digital Multimeter", "Multimeter", "Aplab", "1085", "", ""),
    31: ("Digital Multimeter", "Multimeter", "Aplab", "1085", "", ""),
    32: ("Digital Multimeter 4½-digit", "Multimeter", "", "9030 / 3100", "4½ digit", "Manufacturer not stated in source"),
    33: ("Digital IC Tester", "IC Tester", "Vinytics", "", "Digital ICs", ""),
    34: ("Analog IC Tester", "IC Tester", "Vinytics", "VPL", "Analog ICs", ""),
    35: ("IC Tester", "IC Tester", "", "VPL", "", ""),
    36: ("8085 Microprocessor Kit with Power Supply", "Microprocessor Kit", "", "8085", "Includes power supply", ""),
    37: ("Microprocessor Kit 8503 with Power Supply", "Microprocessor Kit", "", "8503", "Includes power supply", "Model number as written in source"),
    38: ("Microprocessor Kit 8603 with Power Supply", "Microprocessor Kit", "", "8603", "Includes power supply", "Model number as written in source"),
    39: ("Microcontroller Device", "Microcontroller Trainer", "", "MDC-01", "", "Listed in source as 18 separate single-unit rows (rows 39-56)"),
    57: ("8085 Training Kit", "Microprocessor Kit", "Kitek", "8085", "", ""),
    58: ("Digital Trainer Kit", "Digital Trainer Kit", "Vinytics", "", "", ""),
    59: ("Digital Trainer Kit", "Digital Trainer Kit", "Vinytics", "", "", ""),
    60: ("Digital Trainer Kit (Black)", "Digital Trainer Kit", "Vinytics", "", "", ""),
    61: ("Digital Trainer Kit", "Digital Trainer Kit", "Exel", "DT-AADT", "", ""),
    62: ("DC Power Supply 0-30 V", "Power Supply", "Scientific", "PSD3005", "0-30 V", ""),
    63: ("Multi-output Power Supply", "Power Supply", "Scientific", "D3304", "", ""),
    64: ("Multi-output Power Supply", "Power Supply", "Scientific", "D3304", "", ""),
    65: ("Voltage Stabilizer", "UPS / Stabilizer", "AEC", "", "", ""),
    66: ("Servo Stabilizer", "UPS / Stabilizer", "Sambrass", "180", "", ""),
    67: ("Multi-output DC Power Supply", "Power Supply", "Aplab", "LQ3324", "", ""),
    68: ("UPS", "UPS / Stabilizer", "Enlnova", "X-600", "", ""),
    69: ("UPS", "UPS / Stabilizer", "APC", "", "", ""),
    70: ("Breadboard Trainer", "Digital Trainer Kit", "", "", "", ""),
    71: ("Desktop Computer", "Computer", "HP", "270 PO29IL", "", ""),
    72: ("Printer", "Printer / Projector", "Epson", "L130", "", ""),
    73: ("ADALM2000 Active Learning Module (16-bit pattern generator)", "Pattern Generator", "Analog Devices", "ADALM2000", "16-bit pattern generator", "Manufacturer inferred from model name ADALM-2000"),
    74: ("Online UPS 5 kVA", "UPS / Stabilizer", "Uniline", "", "5 kVA, online", "Rating written as '5 KV' in source"),
    75: ("Digital Storage Oscilloscope 70 MHz", "Oscilloscope", "Tektronix", "TBS1072B-EDU", "70 MHz, 2 channel", ""),
    77: ("DC Power Supply with Dongle", "Power Supply", "Keithley", "2231A-30-3", "Triple channel", ""),
    78: ("FPGA Board", "FPGA Board", "", "", "", "HSN code 8473"),
    79: ("Interfacing System (Desktop)", "Computer", "Dell", "OptiPlex 3070", "", "HSN code 8471"),
    80: ("FPGA Board", "FPGA Board", "", "", "", "HSN code 8473"),
    81: ("Interfacing System (Desktop)", "Computer", "Dell", "OptiPlex 3070", "", "HSN code 8471"),
    82: ("Digital Storage Oscilloscope 70 MHz", "Oscilloscope", "Tektronix", "TBS1072B-EDU", "70 MHz, 2 channel", ""),
    83: ("DC Power Supply with Dongle and Router", "Power Supply", "Keithley", "2231A-30-3", "Triple channel", "Supplied with 1 router"),
    84: ("Multimeter", "Multimeter", "Motwani", "76-2A", "", ""),
    # row 85 ("5+5") is split into two records below
    86: ("Triple Channel DC Power Supply (manual)", "Power Supply", "Keithley", "", "Triple channel", ""),
    87: ("Dual-trace Oscilloscope 70-200 MHz", "Oscilloscope", "", "", "70 MHz to 200 MHz, dual", ""),
    88: ("8085 Microprocessor Kit with Manual", "Microprocessor Kit", "", "8085", "", ""),
    89: ("Projector", "Printer / Projector", "Panasonic", "PT-VW530", "", "Manufacturer inferred from model PTVW530"),
}

MW_CATEGORY = {
    "VNA": "Vector Network Analyzer",
    "RF Signal Sources": "Signal Generator",
    "Spectrum Analyzer": "Spectrum Analyzer",
    "DSO": "Oscilloscope",
    "DC Power Supply": "Power Supply",
    "Anechoic Chamber": "Anechoic Chamber",
    "Fabrication": "PCB Prototyping Machine",
}

# Prof. B. P. Das inventory: item -> (category, manufacturer, model)
BPD_CURATION = {
    "Arduino Uno R3": ("Microcontroller Board", "Arduino", "Uno R3"),
    "5V Relay Module": ("Sensor / Module", "", ""),
    "H-Bridge Stepper Motor Driver": ("Motor / Driver", "", ""),
    "Stepper Motor Driver": ("Motor / Driver", "", ""),
    "Distance Sensor": ("Sensor / Module", "", ""),
    "GPS Module": ("Communication Module", "", ""),
    "SIM900A GSM/GPRS Module": ("Communication Module", "SIMCom", "SIM900A"),
    "PMOD GPS": ("Sensor / Module", "", ""),
    "PMOD WiFi": ("Communication Module", "", ""),
    "PMOD Temp Sensor": ("Sensor / Module", "", ""),
    "PMOD Keypad": ("Sensor / Module", "", ""),
    "PMOD LCD 16x2": ("Sensor / Module", "", ""),
    "MG995 Servo Motor": ("Motor / Driver", "TowerPro", "MG995"),
    "Raspberry Pi 3": ("Single-Board Computer", "Raspberry Pi", "Raspberry Pi 3"),
    "Raspberry Pi Pico 2W": ("Microcontroller Board", "Raspberry Pi", "Pico 2 W"),
    "ESP8266 Wi-Fi Development Board": ("Microcontroller Board", "Espressif (ESP8266)", ""),
    "DragonBoard 410C": ("Single-Board Computer", "Arrow / Qualcomm", "DragonBoard 410c"),
    "CMOD A7-35T FPGA": ("FPGA Board", "Digilent", "Cmod A7-35T"),
    "Arduino Nano 33": ("Microcontroller Board", "Arduino", "Nano 33"),
    "Arduino Due": ("Microcontroller Board", "Arduino", "Due"),
    "Line Following Robot Kit": ("Robot Kit", "", ""),
    "Stepping Motor": ("Motor / Driver", "", ""),
}

CATEGORY_KEYWORDS = {
    "Oscilloscope": ["oscilloscope", "cro", "dso", "waveform", "signals", "measurement"],
    "Function Generator": ["function generator", "signal", "waveform", "fg"],
    "Multimeter": ["multimeter", "dmm", "voltage", "current", "resistance", "measurement"],
    "Power Supply": ["power supply", "dc", "bench supply", "psu"],
    "Vector Network Analyzer": ["vna", "network analyzer", "s-parameters", "rf", "microwave", "antenna"],
    "Signal Generator": ["signal generator", "rf source", "rf", "microwave"],
    "Spectrum Analyzer": ["spectrum analyzer", "rf", "frequency domain"],
    "Anechoic Chamber": ["anechoic chamber", "antenna", "far-field", "radiation pattern", "rf"],
    "PCB Prototyping Machine": ["pcb", "milling", "drilling", "fabrication", "prototyping"],
    "IC Tester": ["ic tester", "integrated circuit", "digital", "analog"],
    "Microprocessor Kit": ["microprocessor", "8085", "assembly", "trainer"],
    "Microcontroller Trainer": ["microcontroller", "trainer", "embedded"],
    "Digital Trainer Kit": ["digital electronics", "trainer", "logic gates", "breadboard"],
    "UPS / Stabilizer": ["ups", "stabilizer", "power backup"],
    "Computer": ["computer", "desktop", "pc"],
    "Printer / Projector": ["printer", "projector"],
    "Pattern Generator": ["pattern generator", "logic analyzer", "oscilloscope", "usb instrument"],
    "FPGA Board": ["fpga", "verilog", "vhdl", "digital design"],
    "Microcontroller Board": ["microcontroller", "arduino", "embedded", "iot"],
    "Single-Board Computer": ["single-board computer", "linux", "embedded", "iot"],
    "Sensor / Module": ["sensor", "module", "embedded", "iot"],
    "Communication Module": ["wireless", "communication", "gsm", "gps", "wifi", "iot"],
    "Motor / Driver": ["motor", "driver", "actuator", "robotics"],
    "Robot Kit": ["robot", "robotics", "line follower"],
    "Laser": ["laser", "optics", "optoelectronics", "photonics"],
    "Vacuum Pump": ["vacuum", "pump"],
    "Experiment Set-up": ["experiment", "set-up", "solid state", "semiconductor", "physics"],
    "Optical Measurement": ["optical", "photodetector", "optoelectronics", "measurement"],
    "Gaussmeter": ["gaussmeter", "magnetic field", "hall probe", "measurement"],
    "Electromagnet": ["electromagnet", "magnetic field", "hall effect", "magnetoresistance"],
    "Source Measure Unit": ["smu", "source measure unit", "i-v characterization", "semiconductor", "measurement"],
    "LCR Meter": ["lcr meter", "impedance", "capacitance", "inductance", "c-v", "measurement"],
    "Spectrometer": ["spectrometer", "spectroscopy", "optics", "wavelength"],
    "Data Acquisition": ["data acquisition", "daq", "pxi", "national instruments"],
    "Temperature Instrument": ["temperature", "thermal", "measurement"],
    "Probe Station / Chamber": ["chamber", "probe station", "semiconductor", "characterization"],
}

# One original, representative illustration per category (static/img/equipment/*.svg)
CATEGORY_IMAGE = {
    "Oscilloscope": "oscilloscope.svg",
    "Function Generator": "function-generator.svg",
    "Signal Generator": "signal-generator.svg",
    "Multimeter": "multimeter.svg",
    "Power Supply": "power-supply.svg",
    "Vector Network Analyzer": "network-analyzer.svg",
    "Spectrum Analyzer": "spectrum-analyzer.svg",
    "Anechoic Chamber": "anechoic-chamber.svg",
    "PCB Prototyping Machine": "pcb-machine.svg",
    "IC Tester": "ic-tester.svg",
    "Microprocessor Kit": "trainer-kit.svg",
    "Microcontroller Trainer": "trainer-kit.svg",
    "Digital Trainer Kit": "trainer-kit.svg",
    "UPS / Stabilizer": "ups.svg",
    "Computer": "computer.svg",
    "Printer / Projector": "projector.svg",
    "Pattern Generator": "pattern-generator.svg",
    "FPGA Board": "fpga-board.svg",
    "Microcontroller Board": "mcu-board.svg",
    "Single-Board Computer": "sbc.svg",
    "Sensor / Module": "sensor-module.svg",
    "Communication Module": "comm-module.svg",
    "Motor / Driver": "motor.svg",
    "Robot Kit": "robot.svg",
    "Laser": "laser.svg",
    "Vacuum Pump": "vacuum-pump.svg",
    "Experiment Set-up": "experiment-setup.svg",
    "Optical Measurement": "bench-instrument.svg",
    "Gaussmeter": "bench-instrument.svg",
    "Electromagnet": "experiment-setup.svg",
    "Source Measure Unit": "bench-instrument.svg",
    "LCR Meter": "bench-instrument.svg",
    "Spectrometer": "spectrometer.svg",
    "Data Acquisition": "bench-instrument.svg",
    "Temperature Instrument": "bench-instrument.svg",
    "Probe Station / Chamber": "experiment-setup.svg",
}


def _norm(text):
    return " ".join(str(text).split())


def load_del_lab():
    path = os.path.join(SOURCE_DIR, "instrument DEL Lab.xlsx")
    ws = openpyxl.load_workbook(path).active
    groups = []  # [rows, text, qty]
    for i, (c, d, _e) in enumerate(ws.iter_rows(min_col=3, max_col=5, values_only=True), 1):
        if c is None and d is None:
            continue
        text = _norm(c)
        if d is None and groups:  # continuation line of a merged cell
            groups[-1][0].append(i)
            groups[-1][1] += " " + text
        else:
            groups.append([[i], text, d])

    # Consolidate consecutive identical single-unit rows (MDC-01 is listed once per unit)
    merged = []
    for g in groups:
        prev = merged[-1] if merged else None
        if prev and prev[1] == g[1] and g[2] == 1 and prev[3]:
            prev[0].extend(g[0])
            prev[2] += 1
        else:
            merged.append([list(g[0]), g[1], g[2], g[2] == 1])

    records = []
    for rows, text, qty, _single in merged:
        first = rows[0]
        ref = f"rows {rows[0]}-{rows[-1]}" if len(rows) > 1 else f"row {first}"
        if first == 85:  # "TBS 1072B ... DSO (70MHz) With A.F.G 1022 ..." qty "5+5"
            a, b = [int(x) for x in str(qty).split("+")]
            records.append(dict(name="Digital Storage Oscilloscope 70 MHz (bundled with AFG1022)",
                                category="Oscilloscope", manufacturer="Tektronix", model="TBS1072B",
                                specification="70 MHz, 2 channel", quantity=a, source_text=text,
                                source_ref=ref, notes="Source quantity '5+5' split: 5 DSOs + 5 function generators"))
            records.append(dict(name="Arbitrary Function Generator 25 MHz (bundled with TBS1072B)",
                                category="Function Generator", manufacturer="Tektronix", model="AFG1022",
                                specification="25 MHz, dual channel", quantity=b, source_text=text,
                                source_ref=ref, notes="Source quantity '5+5' split: 5 DSOs + 5 function generators"))
            continue
        cur = DEL_CURATION[first]
        name, cat, mfr, model, spec, note = cur
        records.append(dict(name=name, category=cat, manufacturer=mfr, model=model, specification=spec,
                            quantity=int(qty), source_text=text, source_ref=ref, notes=note))
    return records


def load_microwave_lab():
    path = os.path.join(SOURCE_DIR, "MW_Lab_Data_transcribed.csv")
    records = []
    with open(path, newline="", encoding="utf-8") as fh:
        for i, row in enumerate(csv.DictReader(fh), 1):
            group = row["Equipment"].strip()
            spec_raw = _norm(row["Specification"])
            cat = MW_CATEGORY[group]
            mfr_match = re.match(r"(R&S|Anritsu|Keysight|Agilent|TETRINIX|LPKF)", spec_raw)
            mfr = {"R&S": "Rohde & Schwarz", "TETRINIX": "Tektronix"}.get(
                mfr_match.group(1), mfr_match.group(1)) if mfr_match else ""
            remark = _norm(row["Usage / Remarks"])
            research_only = "research purpose" in remark.lower()
            spec = re.sub(r"^(R&S Make|Anritsu Make|Keysight Make|Agilent Make|TETRINIX|LPKF)\s*", "", spec_raw)
            spec = (spec.replace("Soure", "Source").replace("1 MH to", "1 MHz to").replace("GHZ", "GHz")
                    .replace("Feald measurment", "field measurement").strip(" ,"))
            model = ""
            if "NGE 100" in spec_raw:
                model, spec = "NGE100", ""
            if "PNA" in spec_raw:
                model = "PNA"
            if "LPKF S100" in spec_raw:
                model, spec = "ProtoMat S100", "Milling, drilling, cutting, tracing"
            spec = spec.replace("(PNA)", "").replace("Upto", "Up to").strip("() ")
            name = {
                "Vector Network Analyzer": "Vector Network Analyzer",
                "Signal Generator": "Vector Signal Generator" if "Vector" in spec_raw else "RF Signal Generator",
                "Spectrum Analyzer": "Spectrum Analyzer",
                "Oscilloscope": "Digital Storage Oscilloscope",
                "Power Supply": "DC Power Supply",
                "Anechoic Chamber": "Anechoic Chamber (far-field)",
                "PCB Prototyping Machine": "PCB Prototyping Machine",
            }[cat]
            spec = spec.replace("Vector Signal Source", "").replace("Signal Source", "").strip(" ()")
            records.append(dict(
                name=name, category=cat, manufacturer=mfr, model=model, specification=spec,
                quantity=int(row["Quantity"]), source_text=f"{group}: {spec_raw}",
                source_ref=f"Sr. No. {row['Sr. No.']}, line {i}",
                notes="Manufacturer written as 'TETRINIX' in source" if "TETRINIX" in spec_raw else "",
                access_level="Research only" if research_only else "UG/PG Labs & Research",
            ))
    return records


def load_bp_das():
    path = os.path.join(SOURCE_DIR, "Prof_BP_Das_inventory.csv")
    records = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            item = row["Item"].strip()
            cat, mfr, model = BPD_CURATION[item]
            note = (row.get("Notes") or "").strip()
            if note == "old":
                note = "Marked 'old' in source"
            elif note:
                note = note.replace('""', '"')
            records.append(dict(name=item, category=cat, manufacturer=mfr, model=model, specification="",
                                quantity=int(row["Nos."]), source_text=item,
                                source_ref=f"S.No. {row['S.No.']}", notes=note))
    return records


def load_solid_state_lab():
    path = os.path.join(SOURCE_DIR, "Solid State Lab eqp_list_prep_sumit.xlsx")
    ws = openpyxl.load_workbook(path).active
    records = []
    for i, row in enumerate(ws.iter_rows(min_row=2, max_col=9, values_only=True), 2):
        sn, _date, item, mfr_src, model_src, qty, _stock, _sign, place = row
        if item is None or sn is None:
            continue
        name, cat, mfr, model, spec, note = SSL_CURATION[int(sn)]
        place = _norm(place or "")
        where = ("Location: rack (written 'RECK' in source)" if place.upper() == "RECK"
                 else f"Location: table {place}" if place else "")
        records.append(dict(name=name, category=cat, manufacturer=mfr, model=model, specification=spec,
                            quantity=int(qty), source_text=_norm(item), source_ref=f"S.N. {sn}, row {i}",
                            notes="; ".join(x for x in (where, note) if x)))
    return records


LOADERS = {"del-lab": load_del_lab, "microwave-lab": load_microwave_lab, "prof-bp-das": load_bp_das,
           "solid-state-lab": load_solid_state_lab}


def all_records():
    return {code: fn() for code, fn in LOADERS.items()}


def seed(db, models):
    """Insert every lab in LABS that is not yet in the database (with its equipment). Idempotent:
    existing labs, users, bookings and staff edits are never touched."""
    Lab, Equipment, Keyword = models.Lab, models.Equipment, models.Keyword
    existing = {code for (code,) in db.session.query(Lab.code)}
    missing = [l for l in LABS if l["code"] not in existing]
    if not missing:
        return False
    kw_cache = {}

    def kw(word):
        if word not in kw_cache:
            kw_cache[word] = Keyword.query.filter_by(word=word).first() or Keyword(word=word)
        return kw_cache[word]

    data = {l["code"]: LOADERS[l["code"]]() for l in missing}
    with db.session.no_autoflush:
        _insert(db, Lab, Equipment, data, kw, missing)
    db.session.commit()
    return True


def _insert(db, Lab, Equipment, data, kw, labs=LABS):
    for lab_info in labs:
        lab = Lab(code=lab_info["code"], name=lab_info["name"], description=lab_info["description"],
                  source_file=lab_info["source_file"], room_number=None)
        db.session.add(lab)
        for rec in data[lab_info["code"]]:
            eq = Equipment(
                lab=lab, name=rec["name"], category=rec["category"], manufacturer=rec["manufacturer"] or None,
                model=rec["model"] or None, specification=rec["specification"] or None,
                quantity=rec["quantity"], condition="Unknown",
                access_level=rec.get("access_level", "Not specified"),
                notes=rec["notes"] or None, source_text=rec["source_text"], source_ref=rec["source_ref"],
                image_path="img/equipment/" + CATEGORY_IMAGE.get(rec["category"], "fallback.svg"),
                image_kind="illustration",
            )
            words = set(CATEGORY_KEYWORDS.get(rec["category"], []))
            for extra in (rec["manufacturer"], rec["model"]):
                if extra:
                    words.add(extra.lower())
            eq.keywords = [kw(w) for w in sorted(words)]
            db.session.add(eq)
