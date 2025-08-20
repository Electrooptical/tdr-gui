# Live Plotting Tool for the TDR01

## Introduction
The current tool is a basic live plotting function which communicates with the TDR01 serial interface with VISA.
The plotting function includes saving traces, storing traces for comparison, and cursor annotations.

![Live Plotting Tool for the TDR01](trace-screenshot.png)

## Installation & Use 
The live trace plot is a python package in a TKinter environment.
The tool has been tested on Windows 11 and Ubuntu 24.04 with Ubuntu as the primary platform.

### Ubuntu Dependencies
```bash
apt-get update
apt-get install -y \
    python3-tk \
    tk \
    x11-utils \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxext6 \
    libxrender1 \
    libxrandr2 \
    libxi6
```
### Use
```bash
monitor_tdr --device /dev/ttyUSB0
```

```bash
monitor_tdr --help

Usage: monitor_tdr [OPTIONS]

Options:
  --sleep FLOAT        Sleep time in between traces
  --m FLOAT
  --rc FLOAT
  --start_time FLOAT
  --ramp_mode INTEGER
  --spacing INTEGER
  --maxtime INTEGER
  --device TEXT
  --help               Show this message and exit.
```

## Precompiled Binaries
Precompiled binaries are available under releases.

```bash
dist/monitor_tdr --device /dev/ttyUSB0
```

### Building Binaries
First install in a clean virtual environment and run:
```bash
pyinstaller pyinstaller.spec
```


## Installation From Source
If installing locally then a virtual environment or anaconda should be used.

```bash
pip install .
```

