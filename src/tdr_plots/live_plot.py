from .tdr01_control.common import TraceSettings
from .tdr01_control.common import Adc, TimingParams, MAX_RAMP_INDEX
from .tdr01_control.control import Device
from .tdr01_control import control
from typing import List, Union, Optional
import logging
import queue
import time
import threading
import csv
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib import animation
from matplotlib.lines import Line2D
from matplotlib.ticker import MaxNLocator
import mplcursors


plt.style.use(["dark_background"])  # , "presentation"])
_FRAME_TITLE = "ElectroOptical Innovations: TDR01 Time Domain Reflectometer"
GRID_COLOR = (0, 1, 0, 0.1)
CURSOR_COLOR = (0, 1, 0, 0.75)
TRACE_COLOR = (0, 1, 0, 0.75)
LABEL_FONTSIZE = 12

_PANEL_BG = "#1e1e1e"
_PANEL_FG = "#e0e0e0"
_PANEL_FIELD_BG = "#2d2d2d"

log_ = logging.getLogger("monitor_tdr")


def _apply_dark_theme(root):
    """Give the ttk sidebar a look that's roughly consistent with the
    matplotlib "dark_background" style used for the plot."""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    root.configure(bg=_PANEL_BG)
    style.configure(".", background=_PANEL_BG, foreground=_PANEL_FG,
                     fieldbackground=_PANEL_FIELD_BG)
    for widget in ("TFrame", "TLabel", "TLabelframe", "TLabelframe.Label"):
        style.configure(widget, background=_PANEL_BG, foreground=_PANEL_FG)
    style.configure("TButton", background=_PANEL_FIELD_BG, foreground=_PANEL_FG)
    style.map("TButton", background=[("active", "#3d3d3d")])
    style.configure("TScale", background=_PANEL_BG, troughcolor=_PANEL_FIELD_BG)
    style.configure(
        "Vertical.TScrollbar", background=_PANEL_FIELD_BG, troughcolor=_PANEL_BG,
        arrowcolor=_PANEL_FG, bordercolor=_PANEL_BG,
    )
    style.map("Vertical.TScrollbar", background=[("active", "#3d3d3d")])
    style.configure("TCombobox", fieldbackground=_PANEL_FIELD_BG, foreground=_PANEL_FG)
    # The "clam" theme pulls a readonly Combobox's field/text colors from its
    # state map rather than the plain configure() above, so without this the
    # Ramp Mode dropdown renders as blank/unreadable text-on-matching-background
    # (confirmed visually: the box looked empty even though it had a value).
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", _PANEL_FIELD_BG), ("disabled", _PANEL_FIELD_BG)],
        foreground=[("readonly", _PANEL_FG), ("disabled", _PANEL_FG)],
        selectbackground=[("readonly", _PANEL_FIELD_BG)],
        selectforeground=[("readonly", _PANEL_FG)],
    )
    # Same "clam" quirk applies to Entry: plain configure() alone doesn't
    # reliably win over the theme's built-in state colors, so the per-slider
    # value entries would otherwise show as light-on-light/unreadable.
    style.configure(
        "TEntry", fieldbackground=_PANEL_FIELD_BG, foreground=_PANEL_FG,
        insertcolor=_PANEL_FG,
    )
    style.map(
        "TEntry",
        fieldbackground=[("disabled", _PANEL_FIELD_BG), ("!disabled", _PANEL_FIELD_BG)],
        foreground=[("disabled", "#808080"), ("!disabled", _PANEL_FG)],
        selectbackground=[("!disabled", "#3d3d3d")],
        selectforeground=[("!disabled", _PANEL_FG)],
    )


def _parse_num(value, cast, default):
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def create_styled_button(
    ax,
    label,
    on_click_function,
    color="grey",
    hover_color="skyblue",
):
    """Deprecated: buttons now live in the ttk SettingsPanel. Kept only in
    case a caller still expects a matplotlib-axes button."""
    from matplotlib.widgets import Button

    button = Button(ax, label)
    button.color = color
    button.hovercolor = hover_color
    button.label.set_fontsize(10)
    button.label.set_fontweight("bold")
    button.label.set_color("black")
    button.label.set_wrap(True)
    button.on_clicked(on_click_function)
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
        self.device: Device = device
        self.data_queue = data_queue
        self.settings = settings
        self.sleep_time = kwargs.get("sleep_time", 0)
        self.thread = None
        self.stop_event = threading.Event()
        # Edge-triggered ("error"/"ok") status changes for the SettingsPanel
        # to surface, so a failing acquisition loop is visible instead of
        # just a plot that silently stops moving.
        self.status_queue = queue.Queue()

    def is_alive(self):
        return self.thread and self.thread.is_alive()

    def trace_thread(self):
        had_error = False
        while not self.stop_event.is_set():
            try:
                trace = control.take_trace(
                    self.device, npoints=self.settings.npoints)
                trace = [int(pt) for pt in trace]
                log_.debug(trace)
                self.data_queue.put(trace)
                if had_error:
                    had_error = False
                    self.status_queue.put(("ok", None))
                time.sleep(self.sleep_time)

            except (AssertionError, ValueError) as e:
                # A single malformed/short trace (e.g. mid-reconfiguration
                # while another read was in flight) - log and retry.
                log_.warning(str(e))
            except Exception as e:
                # Any other failure (device I/O error, timeout, disconnect)
                # used to propagate out of this loop uncaught, silently
                # killing the background thread while the Start/Stop button
                # kept showing "Stop" and the plot just stopped updating
                # with no indication why. Log it, tell the GUI, and keep
                # retrying so the loop self-heals if the device comes back.
                log_.warning("Trace acquisition error: %s", e)
                if not had_error:
                    had_error = True
                    self.status_queue.put(("error", str(e)))
                time.sleep(max(self.sleep_time, 0.5))

    def stop(self):
        if self.is_alive():
            log_.info("Stop thread")
            self.stop_event.set()
            if self.thread:
                self.thread.join()
            log_.info("Thread stopped")

    def start(self):
        # self.stop()
        self.stop_event.clear()
        if not self.is_alive():
            log_.info("Start thread")

            self.thread = threading.Thread(target=self.trace_thread)
            self.thread.daemon = (
                True  # Ensure thread closes when the main program exits
            )
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
        # The (1, 3) V default doesn't match every device/setting combo -
        # on the real TDR01 tested here it read ~0.37V after averaging, so
        # the trace was drawn entirely outside the fixed default range and
        # the plot looked empty even while data was actively streaming.
        # Auto-fit the Y axis once from the first real frame of each new
        # stream (mirrors the existing one-shot xlim behavior below), then
        # leave it alone so manual zoom/pan isn't fought every frame.
        self.need_yscale = True
        # Queue to get data from the emitter thread
        self.data_queue = data_queue
        self.annotations = []  # List to store annotations
        self.ax.grid(True, color=GRID_COLOR, linestyle="--", linewidth=0.5)
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
                    min(xlim) + xspan * 0.25, color=CURSOR_COLOR, linestyle="--", lw=1.5
                ),
                self.ax.axvline(
                    min(xlim) + xspan * 0.75, color=CURSOR_COLOR, linestyle="--", lw=1.5
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
            ".--",
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
            self.ax.set_xlabel("Ramp DAC Setting", fontsize=LABEL_FONTSIZE)
        else:
            t = np.array(range(len(y))) * self.dt
            self.ax.set_xlabel("Time (ps)", fontsize=LABEL_FONTSIZE)

        self.line.set_data(t, y)
        self.line.set_color(TRACE_COLOR)
        if self.xlim is None:
            self.xlim = [0, max(t) + abs(max(t)) / 50]
            self.ax.set_xlim(*self.xlim)
            self.on_xlim_change(self.ax)
        if self.need_yscale and len(y):
            lo, hi = float(np.min(y)), float(np.max(y))
            pad = (hi - lo) * 0.1 or 0.05
            self.ax.set_ylim(lo - pad, hi + pad)
            self.need_yscale = False
        return self.line, *self.stored_lines

    def on_use_volts(self, *args):
        self.plot_volts = not self.plot_volts
        self.xlim = None
        self.need_yscale = True

        for line in self.stored_lines + [self.line]:
            y = line.get_ydata()
            t = self.rxdac if self.plot_volts else np.asarray(
                range(len(y))) * self.dt
            line.set_xdata(t)

        if self.xlim is None:
            self.xlim = [0, max(t) + abs(max(t)) / 50]
            self.ax.set_xlim(*self.xlim)
            self.on_xlim_change(self.ax)

        plt.draw()

    def on_xlim_change(self, ax):
        """Update the X-ticks when the X-axis limits change (due to zoom)."""
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


class LabeledSlider(ttk.Frame):
    """A ttk.Scale with a text label and an editable numeric entry field.
    Dragging the slider updates the entry; typing a value into the entry
    and pressing Enter (or tabbing away) moves the slider to match, so
    exact values can be set directly instead of only by dragging."""

    def __init__(self, parent, label, from_, to, initial, is_int=True,
                 on_change=None, **kwargs):
        super().__init__(parent)
        self.is_int = is_int
        self.on_change = on_change
        self.var = tk.DoubleVar(value=initial)
        self.entry_var = tk.StringVar(value=self._fmt(initial))

        ttk.Label(self, text=label, width=14, anchor="w").pack(side=tk.LEFT)
        self.scale = ttk.Scale(
            self, from_=from_, to=to, orient=tk.HORIZONTAL,
            variable=self.var, command=self._on_scale_change,
        )
        self.scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        self.entry = ttk.Entry(self, textvariable=self.entry_var, width=9, justify=tk.RIGHT)
        self.entry.pack(side=tk.LEFT)
        self.entry.bind("<Return>", self._on_entry_commit)
        self.entry.bind("<KP_Enter>", self._on_entry_commit)
        self.entry.bind("<FocusOut>", self._on_entry_commit)

    def _fmt(self, v):
        return str(int(round(v))) if self.is_int else f"{v:.4g}"

    def _on_scale_change(self, value):
        v = float(value)
        if self.is_int:
            v = round(v)
            self.var.set(v)
        self.entry_var.set(self._fmt(v))
        if self.on_change:
            self.on_change(v)

    def _on_entry_commit(self, event=None):
        """Parse the typed text and move the slider to match. Invalid text
        (empty, non-numeric) is silently reverted to the last good value
        rather than raising, since this also fires on every focus-out."""
        text = self.entry_var.get().strip()
        try:
            v = float(text)
        except ValueError:
            self.entry_var.set(self._fmt(self.get()))
            return
        lo, hi = float(self.scale.cget("from")), float(self.scale.cget("to"))
        clamped = min(max(v, lo), hi)
        self.set(clamped)
        if self.on_change:
            self.on_change(self.get())

    def get(self):
        v = self.var.get()
        return int(round(v)) if self.is_int else v

    def set(self, value):
        if self.is_int:
            value = round(value)
        self.var.set(value)
        self.entry_var.set(self._fmt(value))

    def set_range(self, to):
        """Shrink/grow the scale's upper bound, clamping the current value
        (and never going below the scale's own lower bound)."""
        to = max(float(self.scale.cget("from")), to)
        self.scale.configure(to=to)
        if self.get() > to:
            self.set(to)


class VerticalScrolledFrame(ttk.Frame):
    """A ttk.Frame that scrolls vertically once its content is taller than
    the available space. The settings sidebar (Device info + Trace Settings
    + Timing + Actions) is taller than a lot of screens/window sizes, which
    silently clips the bottom buttons (Save CSV, Clear Annotations, ...)
    with no way to reach them. Widgets go in `.interior`, not directly in
    this frame."""

    def __init__(self, parent, *args, **kwargs):
        super().__init__(parent, *args, **kwargs)

        self.canvas = tk.Canvas(
            self, background=_PANEL_BG, highlightthickness=0, bd=0
        )
        scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.interior = ttk.Frame(self.canvas)
        self._interior_id = self.canvas.create_window(
            (0, 0), window=self.interior, anchor="nw"
        )

        self.interior.bind("<Configure>", self._on_interior_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", self._bind_mousewheel)
        self.canvas.bind("<Leave>", self._unbind_mousewheel)

    def _on_interior_configure(self, event):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        # Keep the interior frame's width equal to the canvas's, so widgets
        # inside it that fill=tk.X actually reach the visible right edge
        # instead of being sized by their own (irrelevant) requested width.
        self.canvas.itemconfigure(self._interior_id, width=event.width)

    # Mouse wheel is only bound while the pointer is over the sidebar so it
    # doesn't hijack scrolling anywhere else in the window.
    def _bind_mousewheel(self, event):
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind_all("<Button-4>", self._on_mousewheel_linux)
        self.canvas.bind_all("<Button-5>", self._on_mousewheel_linux)

    def _unbind_mousewheel(self, event):
        self.canvas.unbind_all("<MouseWheel>")
        self.canvas.unbind_all("<Button-4>")
        self.canvas.unbind_all("<Button-5>")

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_mousewheel_linux(self, event):
        self.canvas.yview_scroll(-1 if event.num == 4 else 1, "units")


class SettingsPanel:
    """Sidebar with device info labels and sliders for the trace/timing
    parameters, wired to write straight to the device via control.setup()/
    control.set_timing()."""

    # header key -> (getter, caster, fallback)
    _INFO_ORDER = [
        "*IDN?",
        "MEASURE:TEMPERATURE:TEMP?",
        "AVG?",
        "TIMING:RAMP?",
        "TIMING:ISTART?",
        "TIMING:RESOLUTION?",
        "TIMING:POINTS?",
        "PULSES?",
        "VTX?",
        "TIMING:AMPLITUDE?",
        "TIMING:RC?",
        "TIMING:B?",
        "TIMING:M?",
        "SLbias?",
        "SBbias?",
    ]

    def __init__(self, parent, device, settings: TraceSettings, emitter_thread,
                 scope: Scope, header: Optional[dict] = None):
        self.device = device
        self.settings = settings
        self.emitter_thread = emitter_thread
        self.scope = scope

        if header is None and device is not None:
            try:
                header = control.setup(device, settings)
            except Exception as e:
                log_.warning("Could not query device on startup: %s", e)
                header = {}
        header = header or {}

        self.frame = ttk.Frame(parent, padding=8)

        # "Apply Trace Settings" alone takes 3+ seconds round-trip over the
        # TCP UART bridge (measured against the real device: ~14 queries + 9
        # writes, each a network round trip). Run it off the Tk main thread
        # so the plot/window don't freeze, and show what's happening.
        self._busy = False
        self.status_var = tk.StringVar(value="Idle")
        status_row = ttk.Frame(self.frame)
        status_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(status_row, text="Status:", width=8, anchor="w").pack(side=tk.LEFT)
        ttk.Label(status_row, textvariable=self.status_var, anchor="w").pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )

        info_box = ttk.LabelFrame(self.frame, text="Device", padding=6)
        info_box.pack(fill=tk.X, pady=(0, 8))
        self.info_var = tk.StringVar(value=self._format_info(header))
        ttk.Label(info_box, textvariable=self.info_var, justify=tk.LEFT).pack(anchor="w")
        self.query_btn = ttk.Button(info_box, text="Query Device", command=self.on_query)
        self.query_btn.pack(fill=tk.X, pady=(4, 0))

        trace_box = ttk.LabelFrame(self.frame, text="Trace Settings", padding=6)
        trace_box.pack(fill=tk.X, pady=(0, 8))

        avg0 = _parse_num(header.get("AVG?"), int, settings.naverages)
        spacing0 = _parse_num(header.get("TIMING:RESOLUTION?"), int, settings.spacing)
        istart0 = _parse_num(header.get("TIMING:ISTART?"), int, settings.i_start)
        npoints0 = _parse_num(header.get("TIMING:POINTS?"), int, settings.npoints)
        pulses0 = _parse_num(header.get("PULSES?"), int, settings.pulses)
        vbtx0 = _parse_num(header.get("VTX?"), int, settings.vbtx or 0)
        sl_bias0 = _parse_num(header.get("SLbias?"), int, settings.sl_bias)
        sb_bias0 = _parse_num(header.get("SBbias?"), int, settings.sb_bias)
        ramp0 = header.get("TIMING:RAMP?") or settings.ramp_mode
        if ramp0 not in ("RAMP1", "RAMP2", "BOTH"):
            ramp0 = settings.ramp_mode

        self.averages = LabeledSlider(trace_box, "Averages", 1, 100, avg0)
        self.averages.pack(fill=tk.X)
        self.spacing = LabeledSlider(trace_box, "Spacing (ps)", 1, 2000, spacing0)
        self.spacing.pack(fill=tk.X)
        # i_start and npoints share the ramp's 16-bit index range (see
        # MAX_RAMP_INDEX): npoints is created first so _on_i_start_change can
        # reference it, but still packed after i_start to keep this order in
        # the UI.
        self.npoints = LabeledSlider(trace_box, "Points", 10, MAX_RAMP_INDEX, npoints0)
        self.i_start = LabeledSlider(
            trace_box, "Start Index", 0, MAX_RAMP_INDEX - 10, istart0,
            on_change=self._on_i_start_change,
        )
        self.i_start.pack(fill=tk.X)
        self.npoints.pack(fill=tk.X)
        self._on_i_start_change(istart0)
        self.pulses = LabeledSlider(trace_box, "Pulses", 1, 255, pulses0)
        self.pulses.pack(fill=tk.X)
        self.vbtx = LabeledSlider(trace_box, "VBTX (dac)", 0, 65535, vbtx0)
        self.vbtx.pack(fill=tk.X)
        self.sl_bias = LabeledSlider(trace_box, "SLBias (dac)", 0, 4095, sl_bias0)
        self.sl_bias.pack(fill=tk.X)
        self.sb_bias = LabeledSlider(trace_box, "SBBias (dac)", 0, 4095, sb_bias0)
        self.sb_bias.pack(fill=tk.X)

        ramp_row = ttk.Frame(trace_box)
        ramp_row.pack(fill=tk.X, pady=2)
        ttk.Label(ramp_row, text="Ramp Mode", width=14, anchor="w").pack(side=tk.LEFT)
        self.ramp_mode = tk.StringVar(value=ramp0)
        ttk.Combobox(
            ramp_row, textvariable=self.ramp_mode,
            values=["RAMP1", "RAMP2", "BOTH"], state="readonly", width=10,
        ).pack(side=tk.LEFT)

        self.apply_trace_btn = ttk.Button(
            trace_box, text="Apply Trace Settings", command=self.on_apply_trace
        )
        self.apply_trace_btn.pack(fill=tk.X, pady=(6, 0))

        timing_box = ttk.LabelFrame(self.frame, text="Timing (Ramp Fit)", padding=6)
        timing_box.pack(fill=tk.X, pady=(0, 8))

        a0 = _parse_num(header.get("TIMING:AMPLITUDE?"), float, 60075)
        rc0 = _parse_num(header.get("TIMING:RC?"), float, 16500)
        bf0 = _parse_num(header.get("TIMING:B?"), float, 0)
        m0 = _parse_num(header.get("TIMING:M?"), float, 0)

        self.a = LabeledSlider(timing_box, "Amplitude (a)", 0, 65535, a0)
        self.a.pack(fill=tk.X)
        self.rc = LabeledSlider(timing_box, "RC", 1, 200000, rc0, is_int=False)
        self.rc.pack(fill=tk.X)
        self.bf = LabeledSlider(timing_box, "Offset (b)", 0, 65535, bf0)
        self.bf.pack(fill=tk.X)
        self.m = LabeledSlider(timing_box, "Slope (m)", -10, 10, m0, is_int=False)
        self.m.pack(fill=tk.X)

        self.apply_timing_btn = ttk.Button(
            timing_box, text="Apply Timing", command=self.on_apply_timing
        )
        self.apply_timing_btn.pack(fill=tk.X, pady=(6, 0))

        actions_box = ttk.LabelFrame(self.frame, text="Actions", padding=6)
        actions_box.pack(fill=tk.X)
        self.start_stop_var = tk.StringVar(value="Start")
        ttk.Button(
            actions_box, textvariable=self.start_stop_var, command=self.on_start_stop
        ).pack(fill=tk.X, pady=2)
        ttk.Button(actions_box, text="Store", command=scope.store).pack(fill=tk.X, pady=2)
        ttk.Button(actions_box, text="Clear Stored", command=scope.clear_stored).pack(
            fill=tk.X, pady=2
        )
        ttk.Button(actions_box, text="Save CSV", command=scope.save_csv).pack(
            fill=tk.X, pady=2
        )
        ttk.Button(
            actions_box, text="Clear Annotations", command=scope.clear_annotations
        ).pack(fill=tk.X, pady=2)
        ttk.Button(actions_box, text="Volts/Time", command=scope.on_use_volts).pack(
            fill=tk.X, pady=2
        )
        ttk.Button(actions_box, text="Cursors", command=scope.on_cursors).pack(
            fill=tk.X, pady=2
        )

        if self.emitter_thread.is_alive():
            self.status_var.set("Streaming")
        self.frame.after(300, self._poll_emitter_status)

    def _run_async(self, work, on_done, busy_message="Working…"):
        """Run a blocking device I/O call (`work`, no args) on a background
        thread so the Tk mainloop keeps servicing the plot/window, then
        deliver the result back to `on_done` on the main thread. Device
        calls measured 3+ seconds round trip over the TCP UART bridge;
        without this every Query/Apply click froze the whole app for that
        long. Device access itself is protected by Device.lock, so this is
        safe to run concurrently with the trace-acquisition thread."""
        if self._busy:
            return
        self._busy = True
        self._set_buttons_enabled(False)
        self.status_var.set(busy_message)

        result_q = queue.Queue()

        def runner():
            try:
                result_q.put(("ok", work()))
            except Exception as e:
                result_q.put(("error", e))

        threading.Thread(target=runner, daemon=True).start()

        def poll():
            try:
                status, payload = result_q.get_nowait()
            except queue.Empty:
                self.frame.after(80, poll)
                return
            self._busy = False
            self._set_buttons_enabled(True)
            if status == "error":
                log_.error(str(payload))
                self.status_var.set("Idle")
                messagebox.showerror("Device Error", str(payload))
            else:
                self.status_var.set(
                    "Streaming" if self.emitter_thread.is_alive() else "Idle"
                )
                on_done(payload)

        self.frame.after(80, poll)

    def _set_buttons_enabled(self, enabled: bool):
        state = "!disabled" if enabled else "disabled"
        for btn in (self.query_btn, self.apply_trace_btn, self.apply_timing_btn):
            btn.state([state])

    def _poll_emitter_status(self):
        try:
            while True:
                kind, msg = self.emitter_thread.status_queue.get_nowait()
                if kind == "error":
                    self.status_var.set(f"Acquisition error: {msg}")
                elif not self._busy:
                    self.status_var.set(
                        "Streaming" if self.emitter_thread.is_alive() else "Idle"
                    )
        except queue.Empty:
            pass
        self.frame.after(300, self._poll_emitter_status)

    def _on_i_start_change(self, i_start_value):
        """Keep i_start + npoints within the ramp's index range by shrinking
        the Points slider (and its current value, if needed) as Start Index
        grows."""
        i_start = int(round(i_start_value))
        self.npoints.set_range(MAX_RAMP_INDEX - i_start)

    def _format_info(self, header):
        lines = []
        for key in self._INFO_ORDER:
            if key in header:
                lines.append(f"{key.rstrip('?')}: {header[key]}")
        return "\n".join(lines) if lines else "Not queried yet"

    def on_query(self):
        if self.device is None:
            return
        self._run_async(
            work=lambda: control.setup(self.device, self.settings),
            on_done=lambda header: self.info_var.set(self._format_info(header)),
            busy_message="Querying device…",
        )

    def on_apply_trace(self):
        if self.device is None:
            messagebox.showinfo("No Device", "Not connected to a device (dummy mode).")
            return

        # Slider values are read here on the main thread (cheap, no device
        # I/O); only the device round trip below runs in the background.
        new_settings = TraceSettings(
            naverages=self.averages.get(),
            spacing=self.spacing.get(),
            i_start=self.i_start.get(),
            npoints=self.npoints.get(),
            pulses=self.pulses.get(),
            vbtx=self.vbtx.get(),
            sl_bias=self.sl_bias.get(),
            sb_bias=self.sb_bias.get(),
            ramp_mode=self.ramp_mode.get(),
        )

        def work():
            was_running = self.emitter_thread.is_alive()
            if was_running:
                self.emitter_thread.stop()
            try:
                header = control.setup(self.device, new_settings)
                rxdac = self.device.query_ascii_values("RXDAC?")
            finally:
                if was_running:
                    self.emitter_thread.settings = new_settings
                    self.emitter_thread.start()
            return header, rxdac

        def on_done(result):
            header, rxdac = result
            self.settings = new_settings
            self.scope.settings = new_settings
            self.scope.dt = new_settings.spacing
            self.scope.rxdac = rxdac
            self.scope.xlim = None
            self.scope.need_yscale = True
            self.info_var.set(self._format_info(header))

        self._run_async(work, on_done, busy_message="Applying trace settings…")

    def on_apply_timing(self):
        if self.device is None:
            messagebox.showinfo("No Device", "Not connected to a device (dummy mode).")
            return

        params = TimingParams(
            npoints=self.settings.npoints,
            dt_ps=self.settings.spacing,
            a=self.a.get(),
            rc=self.rc.get(),
            bf=self.bf.get(),
            m=self.m.get(),
        )

        def work():
            control.set_timing(self.device, params)
            return control.setup(self.device, self.settings)

        self._run_async(
            work=work,
            on_done=lambda header: self.info_var.set(self._format_info(header)),
            busy_message="Applying timing…",
        )

    def on_start_stop(self):
        if self.emitter_thread.is_alive():
            self.emitter_thread.stop()
            self.start_stop_var.set("Start")
            if not self._busy:
                self.status_var.set("Idle")
        else:
            self.emitter_thread.start()
            self.start_stop_var.set("Stop")
            if not self._busy:
                self.status_var.set("Streaming")


def run_monitor_plot(
    settings: TraceSettings,
    rxdac: List[int],
    device: Device,
    header: Optional[dict] = None,
):
    root = tk.Tk()
    root.title(_FRAME_TITLE)
    _apply_dark_theme(root)

    data_queue = queue.Queue()
    emitter_thread = EmitterThread(
        data_queue=data_queue, settings=settings, device=device
    )

    main_frame = ttk.Frame(root)
    main_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    plot_frame = ttk.Frame(main_frame)
    plot_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    fig, ax = plt.subplots()
    ax.set_ylabel("RX Volts", fontsize=LABEL_FONTSIZE)

    # Creating the FigureCanvasTkAgg rebinds fig.canvas to this embedded
    # canvas, so Scope's mpl_connect calls (below) must happen after this.
    canvas = FigureCanvasTkAgg(fig, master=plot_frame)
    canvas.draw()

    toolbar = NavigationToolbar2Tk(canvas, plot_frame, pack_toolbar=False)
    toolbar.update()
    toolbar.pack(side=tk.TOP, fill=tk.X)

    canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    scope = Scope(
        ax, dt=settings.spacing, settings=settings, rxdac=rxdac, data_queue=data_queue
    )

    cursor = mplcursors.cursor(scope.line, hover=False, multiple=True)

    def on_add_annotation(sel):
        scope.annotations.append(sel.annotation)

    cursor.connect("add", on_add_annotation)

    sidebar = VerticalScrolledFrame(main_frame)
    sidebar.pack(side=tk.RIGHT, fill=tk.Y)

    panel = SettingsPanel(
        sidebar.interior,
        device=device,
        settings=settings,
        emitter_thread=emitter_thread,
        scope=scope,
        header=header,
    )
    panel.frame.pack(fill=tk.BOTH, expand=True)

    # The canvas has no natural width of its own; size it once to the
    # settings panel's actual requested width so the sidebar keeps the same
    # width it had before scrolling was added, instead of collapsing to the
    # Canvas default (200px) or growing unbounded.
    sidebar.update_idletasks()
    sidebar.canvas.configure(width=panel.frame.winfo_reqwidth())

    def on_close():
        log_.info("Window closing, stopping emitter thread…")
        if emitter_thread.is_alive():
            emitter_thread.stop()
        root.quit()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    #  the animation has to be set as a variable
    ani = animation.FuncAnimation(
        fig, scope.update, interval=200, blit=False, save_count=1000
    )

    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    root.geometry(f"{screen_width}x{screen_height}")

    root.mainloop()
