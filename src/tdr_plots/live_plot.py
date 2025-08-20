from typing import List
import logging
import queue
import time
import random
import threading
import math
import csv
from datetime import datetime

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.widgets import Button
from matplotlib import animation
from matplotlib.lines import Line2D
from matplotlib.ticker import MaxNLocator
import tkinter as tk
from tkinter import filedialog
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import mplcursors

from tdr01_control import control
from tdr01_control.control import Device
from tdr01_control.common import Adc
from tdr01_control.common import TraceSettings

log_ = logging.getLogger("live_plot")


def create_styled_button(
    ax,
    label,
    on_click_function,
    color="lightblue",
    hover_color="skyblue",
    width=0.1,
    height=0.075,
):
    """Create a styled button with hover effect and custom color."""
    button = Button(ax, label)

    # Set button style
    button.color = color
    button.hovercolor = hover_color
    button.label.set_fontsize(12)  # Set font size
    button.label.set_fontweight("bold")  # Make font bold
    button.label.set_color("black")  # Set text color

    # Apply custom behavior for hover effect
    def on_hover(event):
        if button.ax.contains(event)[0]:
            button.color = hover_color
            button.ax.figure.canvas.draw_idle()
        else:
            button.color = color
            button.ax.figure.canvas.draw_idle()

    button.on_clicked(on_click_function)

    # Set up hover effect
    button.ax.figure.canvas.mpl_connect("motion_notify_event", on_hover)

    return button


def save_csv(fname, rxdac, ramp_time, traces):
    with open(fname, "w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        header_row = ["rxdac (dac)", "time (ps)"]
        for i, _ in enumerate(traces):
            header_row.append(f"Trace_{i}")
        writer.writerow(header_row)

        for line in zip(rxdac, ramp_time, *traces):
            writer.writerow(line)
    log_.info(f"Saved trace data to {fname}")


class Scope:
    def __init__(self, ax, dt=10, settings=None, rxdac=None, data_queue=None):
        self.ax = ax
        self.dt = dt
        self.settings = settings or TraceSettings()
        self.rxdac = rxdac
        self.stored_lines = []
        self.line = Line2D([0], [0], marker="o", markersize=3)
        self.ax.add_line(self.line)
        self.default_ylim = (1, 3)
        self.ax.set_ylim(*self.default_ylim)
        # self.ax.set_ylim(-0.1, 4095*2)
        self.xlim = None
        # self.ax.set_xlim(0, 1)
        self.data_queue = data_queue  # Queue to get data from the emitter thread
        self.annotations = []  # List to store annotations
        self.ax.grid()
        self.plot_volts = False
        self.ax.callbacks.connect("xlim_changed", self.on_xlim_change)

    def save_csv(self, *args):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_fname = f"tdr_trace_{timestamp}.csv"
        fname = filedialog.asksaveasfilename(
            title="Select a file",
            initialdir="./",
            initialfile=default_fname,
            defaultextension=".csv",
            filetypes=(
                ("CSV", "*.csv"),
                ("Ascii data", "*.dat"),
                ("Text files", "*.txt"),
                ("All files", "*.*"),
            ),
        )

        if fname:
            traces = [self.line.get_ydata()]
            for trace in self.stored_lines:
                traces.append(trace.get_ydata())
            save_csv(
                fname, rxdac=self.rxdac, ramp_time=self.line.get_xdata(), traces=traces
            )

    def clear_annotations(self, *args):
        """Clear all annotations."""
        for annotation in self.annotations:
            annotation.remove()  # Remove annotations from the plot
            self.annotations = []  # Clear the annotations list
            plt.draw()

    def store(self, *args):
        # self.stored_lines.append(copy.deepcopy(self.line))
        (stored_line,) = self.ax.plot(
            self.line.get_xdata(),
            self.line.get_ydata(),
            ".",
            label=f"Stored Trace {len(self.stored_lines)}",
        )
        self.stored_lines.append(stored_line)
        plt.draw()

    def clear_stored(self, *args):
        """Clear all stored traces."""
        for line in self.stored_lines:
            line.remove()  # Remove the stored lines from the plot
            del line
            self.stored_lines = []  # Clear the stored lines list
            plt.draw()

    def update(self, frame):
        log_.debug("update %d", frame)
        try:
            y = self.data_queue.get_nowait()  # Non-blocking get from the queue
        except queue.Empty:
            return (self.line,)

        adc = Adc()
        y = adc.to_volts(np.array(y)) / self.settings.naverages

        if self.plot_volts:
            t = self.rxdac
            self.ax.set_xlabel("Ramp DAC Setting")
        else:
            t = np.array(range(len(y))) * self.dt
            self.ax.set_xlabel("Time (ps)")

        self.line.set_data(t, y)
        if self.xlim is None:
            self.xlim = [0, max(t) + abs(max(t)) / 50]
            self.ax.set_xlim(*self.xlim)
            self.on_xlim_change(self.ax)
        return self.line, *self.stored_lines

    def on_use_volts(self, *args):
        self.plot_volts = not self.plot_volts
        self.xlim = None
        self.ax.set_ylim(*self.default_ylim)

    def on_xlim_change(self, ax):
        """Update the X-ticks when the X-axis limits change (due to zoom)."""
        xlim = ax.get_xlim()  # Get current x limits
        # spacing = 10**(math.ceil(math.log(max(xlim)-min(xlim), 10)))/100
        # while (max(xlim)-min(xlim))/spacing > 30:
        #    spacing *= 2  # Ensure enough space between ticks
        locator = MaxNLocator(
            integer=False,  # Allows for float ticks
            prune="lower",  # Optional: Prunes lower ticks for a cleaner view
        )
        ax.xaxis.set_major_locator(locator)  # Set the ticks
        ax.figure.canvas.draw_idle()  # Redraw the canvas


class EmitterThread:
    def __init__(self, device: Device, data_queue, settings: TraceSettings, **kwargs):
        self.device = device
        self.data_queue = data_queue
        self.settings = settings
        self.sleep_time = kwargs.get("sleep_time", 0)
        self.thread = None
        self.stop_event = threading.Event()

    def trace_thread(self):
        while not self.stop_event.is_set():
            trace = control.take_trace(self.device, npoints=self.settings.npoints)
            trace = [int(pt) for pt in trace]
            log_.debug(trace)
            self.data_queue.put(trace)
            time.sleep(self.sleep_time)

    def dummy_thread(self):
        """Simulate data reading from a serial port in a separate thread."""
        for _ in range(10):
            # Simulate delay for reading from serial port (10Hz rate)
            time.sleep(1)

            # Simulate reading a random value (replace with serial read)
            trace = [
                int(1 << 16) * random.random() for _ in range(self.settings.npoints)
            ]
            # Put data into the queue (either a real serial read or simulated data)
            self.data_queue.put(trace)
            # data_queue.put('x')  # Put data into the queue (either a real serial read or simulated data)

    def stop(self, *args):
        if self.thread is None or not self.thread.is_alive():
            log_.info("No thread running")
            return

        log_.info("Stop thread")
        self.stop_event.set()
        self.thread.join()
        self.stop_event.clear()
        log_.info("Thread stopped")

    def start(self, *args):
        if self.thread is not None and self.thread.is_alive():
            log_.info("Thread already running")
            return

        log_.info("Start thread")

        self.thread = threading.Thread(target=self.trace_thread)
        self.thread.daemon = True  # Ensure thread closes when the main program exits
        self.thread.start()

    def start_dummy(self, *args):
        if self.thread is not None and self.thread.is_alive():
            return

        log_.info("Start thread")

        self.thread = threading.Thread(target=self.dummy_thread)
        self.thread.daemon = True  # Ensure thread closes when the main program exits
        self.thread.start()


def run_monitor_plot(settings: TraceSettings, rxdac: List[int], device: Device):
    fig, ax = plt.subplots()

    ax.set_ylabel("RX Volts")
    ax.set_xlabel("Offset Time (ps)")
    data_queue = queue.Queue()

    scope = Scope(
        ax, dt=settings.spacing, settings=settings, rxdac=rxdac, data_queue=data_queue
    )
    # Start the emitter thread to simulate serial data reading

    device.flush()
    print(device, device.dev)
    assert device.dev
    emitter_thread = EmitterThread(
        data_queue=data_queue, settings=settings, device=device
    )

    # Enable multiple points selection
    cursor = mplcursors.cursor(scope.line, hover=False, multiple=True)

    def on_add_annotation(sel):
        annotation = sel.annotation
        scope.annotations.append(annotation)

    cursor.connect("add", on_add_annotation)

    # Create a button to view stored traces
    buttons = {}
    for name, func in (
        ("Start", emitter_thread.start),
        ("Stop", emitter_thread.stop),
        ("Store", scope.store),
        ("Clear", scope.clear_stored),
        ("Save CSV", scope.save_csv),
        ("Clear Annotations", scope.clear_annotations),
        ("Volts/Time", scope.on_use_volts),
    ):
        # Position of the button
        button_ax = plt.axes([0.05 + 0.125 * len(buttons), 0.9, 0.1, 0.075])
        buttons[name] = create_styled_button(
            ax=button_ax, label=name, on_click_function=func
        )

    manager = plt.get_current_fig_manager()

    root = tk.Tk()
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    manager.resize(screen_width, screen_height)

    #  the animation has to be set as a variable
    ani = animation.FuncAnimation(
        fig, scope.update, interval=10, blit=False, save_count=1000
    )
    plt.show()
