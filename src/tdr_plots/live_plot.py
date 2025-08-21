from .tdr01_control.common import TraceSettings
from .tdr01_control.common import Adc
from .tdr01_control.control import Device
from .tdr01_control import control
from typing import List, Union
import logging
import queue
import time
import random
import threading
import csv
from datetime import datetime
import tkinter as tk
from tkinter import filedialog

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.widgets import Button
from matplotlib import animation
from matplotlib.lines import Line2D
from matplotlib.ticker import MaxNLocator
import mplcursors
import matplotlib.pyplot as plt


print(plt.style.available)
plt.style.use(["dark_background"])  # , "presentation"])


log_ = logging.getLogger("live_plot")

_FRAME_TITLE = "ElectroOptical Innovations: TDR01 Time Domain Reflectometer"


def create_styled_button(
    ax,
    label,
    on_click_function,
    color="lightblue",
    hover_color="skyblue",
):
    """Create a styled button with hover effect and custom color."""
    button = Button(ax, label)

    # Set button style
    button.color = color
    button.hovercolor = hover_color
    button.label.set_fontsize(12)  # Set font size
    button.label.set_fontweight("bold")  # Make font bold
    button.label.set_color("black")  # Set text color
    button.label.set_wrap(True)

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


def get_filename() -> Union[str, None]:
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
    return fname


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

    def stop(self):
        if self.thread is not None and self.thread.is_alive():
            log_.info("Stop thread")
            self.stop_event.set()
            if self.thread:
                self.thread.join()
            self.stop_event.clear()
            log_.info("Thread stopped")

    def start(self):
        if self.thread is None or not self.thread.is_alive():
            log_.info("Start thread")

            self.thread = threading.Thread(target=self.trace_thread)
            self.thread.daemon = (
                True  # Ensure thread closes when the main program exits
            )
            self.thread.start()

    def start_dummy(self, *args):
        if self.thread is not None and self.thread.is_alive():
            return

        log_.info("Start thread")

        self.thread = threading.Thread(target=self.dummy_thread)
        self.thread.daemon = True  # Ensure thread closes when the main program exits
        self.thread.start()


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
        self.xlim = None
        # Queue to get data from the emitter thread
        self.data_queue = data_queue
        self.annotations = []  # List to store annotations
        self.ax.grid(True, color=(0, 1, 0, 0.1), linestyle="--", linewidth=0.5)
        self.plot_volts = False
        self.ax.callbacks.connect("xlim_changed", self.on_xlim_change)

        self._init_cursors()

    def _init_cursors(self):
        self.cid_press = self.ax.figure.canvas.mpl_connect(
            "button_press_event", self.on_press
        )
        self.cid_release = self.ax.figure.canvas.mpl_connect(
            "button_release_event", self.on_release
        )
        self.cid_motion = self.ax.figure.canvas.mpl_connect(
            "motion_notify_event", self.on_motion
        )

        self.dragging_cursor = None
        self.cursor_lines = []
        self.cursor_text = None

    def on_cursors(self, *args):
        if hasattr(self, "cursor_lines") and len(self.cursor_lines):
            for pt in self.cursor_lines:
                pt.remove()

            self.cursor_lines = []
            self.cursor_text.remove()

        else:
            xlim = self.ax.set_xlim()
            xspan = max(xlim) - min(xlim)
            self.cursor_lines = [
                self.ax.axvline(
                    min(xlim) + xspan * 0.25, color="lightgreen", linestyle="--", lw=1.5
                ),
                self.ax.axvline(
                    min(xlim) + xspan * 0.75, color="lightgreen", linestyle="--", lw=1.5
                ),
            ]
            self.cursor_text = self.ax.text(
                0.7,
                0.95,
                "",
                transform=self.ax.transAxes,
                fontsize=10,
                verticalalignment="top",
                bbox=dict(boxstyle="round", facecolor="wheat", alpha=0.5),
            )

    def save_csv(self, *args):
        fname = get_filename()
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

        # del line
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
        self.line.set_color((0, 1, 0, 0.6))
        if self.xlim is None:
            self.xlim = [0, max(t) + abs(max(t)) / 50]
            self.ax.set_xlim(*self.xlim)
            self.on_xlim_change(self.ax)
        return self.line, *self.stored_lines

    def on_use_volts(self, *args):
        self.plot_volts = not self.plot_volts
        self.xlim = None
        self.ax.set_ylim(*self.default_ylim)

        for line in self.stored_lines + [self.line]:
            y = line.get_ydata()
            if self.plot_volts:
                t = self.rxdac
            else:
                t = np.array(range(len(y))) * self.dt
            line.set_xdata(t)

        if self.xlim is None:
            self.xlim = [0, max(t) + abs(max(t)) / 50]
            self.ax.set_xlim(*self.xlim)
            self.on_xlim_change(self.ax)

        plt.draw()

    def on_xlim_change(self, ax):
        """Update the X-ticks when the X-axis limits change (due to zoom)."""
        # spacing = 10**(math.ceil(math.log(max(xlim)-min(xlim), 10)))/100
        # while (max(xlim)-min(xlim))/spacing > 30:
        #    spacing *= 2  # Ensure enough space between ticks
        locator = MaxNLocator(
            integer=False,  # Allows for float ticks
            prune="lower",  # Optional: Prunes lower ticks for a cleaner view
        )
        ax.xaxis.set_major_locator(locator)  # Set the ticks
        ax.figure.canvas.draw_idle()  # Redraw the canvas

    def on_press(self, event):
        if event.inaxes != self.ax:
            return
        # check if near a cursor line
        for i, line in enumerate(self.cursor_lines):
            x = line.get_xdata()[0]
            # 2% tolerance
            if abs(event.xdata - x) < (self.xlim[1] - self.xlim[0]) / 50:
                self.dragging_cursor = i
                break

    def on_release(self, event):
        self.dragging_cursor = None

    def on_motion(self, event):
        if self.dragging_cursor is None or event.inaxes != self.ax:
            return
        x = event.xdata
        self.cursor_lines[self.dragging_cursor].set_xdata([x, x])
        self.update_cursor_text()
        self.ax.figure.canvas.draw_idle()

    def update_cursor_text(self):
        x1 = self.cursor_lines[0].get_xdata()[0]
        x2 = self.cursor_lines[1].get_xdata()[0]

        # Interpolate y values from main trace
        xdata = self.line.get_xdata()
        ydata = self.line.get_ydata()
        y1 = np.interp(x1, xdata, ydata)
        y2 = np.interp(x2, xdata, ydata)

        dx = x2 - x1
        dy = y2 - y1

        self.cursor_text.set_text(f"Δx={dx:.3f}, Δy={dy:.3f}")


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

    def on_start_stop(*args):
        emitter_thread.stop()
        emitter_thread.start()

    # Create a button to view stored traces
    buttons = {}
    button_bindings = (
        ("Start/Stop", on_start_stop),
        ("Store", scope.store),
        ("Clear", scope.clear_stored),
        ("Save CSV", scope.save_csv),
        ("Clear Annotations", scope.clear_annotations),
        ("Volts/Time", scope.on_use_volts),
        ("Cursors", scope.on_cursors),
    )

    button_xmargin = 0.05
    button_spacing = 0.005
    button_width = (
        1 - button_xmargin * 2 - button_spacing * len(button_bindings)
    ) / len(button_bindings)
    for i, button in enumerate(button_bindings):
        name, func = button
        # Position of the button
        button_ax = plt.axes(
            [
                button_xmargin + (button_spacing + button_width) * i,
                0.9,
                button_width,
                0.05,
            ]
        )
        buttons[name] = create_styled_button(
            ax=button_ax, label=name, on_click_function=func
        )

    manager = plt.get_current_fig_manager()
    manager.set_window_title(_FRAME_TITLE)

    root = tk.Tk()
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    root.destroy()  # Close the Tkinter root window

    manager.resize(screen_width, screen_height)

    #  the animation has to be set as a variable
    try:
        ani = animation.FuncAnimation(
            fig, scope.update, interval=10, blit=False, save_count=1000
        )
        plt.show()
    except Exception as e:
        log_.info(f"CAPTURED EXCEPTION {e}")
        raise
