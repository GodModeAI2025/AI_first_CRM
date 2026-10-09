#!/usr/bin/env python3
"""Charts as inline SVG for the generated CRM dashboards, standard library only.

Pure rendering: callers pass category labels, series values (Decimal or None),
formatted value texts and colour slots; nothing here knows the data model.
Marks follow one rule set: bars at most 24px thick with a 4px rounded data end
and a square baseline, a 2px surface gap between stacked segments, 2px lines
with ringed markers, solid hairline gridlines, and a title and description for
assistive technology. Colours are CSS classes (f0..f7 fill, s0..s7 stroke, fx
and sx for "other"), so light and dark mode swap in the page stylesheet.
Values are Decimal; floats appear only as pixel coordinates.
"""

from __future__ import annotations

import html
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Callable, Optional, Sequence

WIDTH = 640
PLOT_HEIGHT = 220
LABEL_CHARS = 26
MAX_THICKNESS = 24
Formatter = Callable[[Optional[Decimal]], str]


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def num(value: float) -> str:
    """Pixel coordinate with one decimal and no trailing zero, identical on every platform."""
    text = f"{value:.1f}"
    if text.endswith(".0"):
        text = text[:-2]
    return "0" if text == "-0" else text


def slot_class(slot: Any, kind: str = "f") -> str:
    return f"{kind}x" if slot in (None, "x", "other") else f"{kind}{int(slot) % 8}"


def shorten(label: str, limit: int = LABEL_CHARS) -> str:
    return label if len(label) <= limit else label[: limit - 1].rstrip() + "…"


def compact_number(value: Decimal, language: str = "en", formatter: Optional[Callable[[Decimal, Optional[int]], str]] = None) -> str:
    """Axis tick text: 1.2M / 1,2 Mio.; small values in full."""
    fmt = formatter or (lambda number, places: f"{number:,.{places}f}" if places is not None else f"{number:,}")
    german = language == "de"
    magnitude = abs(value)
    for limit, short_en, short_de in ((Decimal(10) ** 9, "B", " Mrd."), (Decimal(10) ** 6, "M", " Mio."), (Decimal(10) ** 3, "K", " Tsd.")):
        if magnitude >= limit:
            scaled = (value / limit).quantize(Decimal("0.1"))
            text = fmt(scaled, 0 if scaled == scaled.to_integral_value() else 1)
            return text + (short_de if german else short_en)
    if value == value.to_integral_value():
        return fmt(value.quantize(Decimal(1)), 0)
    return fmt(value, 2)


def nice_scale(low: Decimal, high: Decimal, ticks: int = 4, *, integer: bool = False) -> tuple[Decimal, Decimal, Decimal]:
    """Rounded axis bounds and step that include zero and every value; counts get whole-number steps."""
    low, high = min(low, Decimal(0)), max(high, Decimal(0))
    if high == low:
        high = low + 1
    raw = (high - low) / ticks
    exponent = raw.adjusted()
    magnitude = Decimal(1).scaleb(exponent)
    step = magnitude * 10
    for factor in (Decimal(1), Decimal(2), Decimal("2.5"), Decimal(5), Decimal(10)):
        if factor * magnitude >= raw:
            step = factor * magnitude
            break
    if integer:
        step = max(Decimal(1), step.to_integral_value(ROUND_CEILING))
    nice_low = (low / step).to_integral_value(ROUND_FLOOR) * step
    nice_high = (high / step).to_integral_value(ROUND_CEILING) * step
    return nice_low, nice_high, step


def _ticks(low: Decimal, high: Decimal, step: Decimal) -> list[Decimal]:
    values = []
    current = low
    while current <= high + step / 1000 and len(values) < 50:
        values.append(current)
        current += step
    return values


def _bar_path(x: float, y_base: float, y_end: float, thickness: float, *, horizontal: bool, rounded: bool) -> str:
    """A bar from its baseline to its data end; only the data end is rounded."""
    if horizontal:
        length = y_end - y_base
        top, bottom = x, x + thickness
        radius = min(4.0, thickness / 2, abs(length)) if rounded else 0.0
        if length >= 0:
            return (f"M{num(y_base)} {num(top)}H{num(y_end - radius)}Q{num(y_end)} {num(top)} {num(y_end)} {num(top + radius)}"
                    f"V{num(bottom - radius)}Q{num(y_end)} {num(bottom)} {num(y_end - radius)} {num(bottom)}H{num(y_base)}Z")
        return (f"M{num(y_base)} {num(top)}H{num(y_end + radius)}Q{num(y_end)} {num(top)} {num(y_end)} {num(top + radius)}"
                f"V{num(bottom - radius)}Q{num(y_end)} {num(bottom)} {num(y_end + radius)} {num(bottom)}H{num(y_base)}Z")
    left, right = x, x + thickness
    length = y_base - y_end
    radius = min(4.0, thickness / 2, abs(length)) if rounded else 0.0
    if length >= 0:
        return (f"M{num(left)} {num(y_base)}V{num(y_end + radius)}Q{num(left)} {num(y_end)} {num(left + radius)} {num(y_end)}"
                f"H{num(right - radius)}Q{num(right)} {num(y_end)} {num(right)} {num(y_end + radius)}V{num(y_base)}Z")
    return (f"M{num(left)} {num(y_base)}V{num(y_end - radius)}Q{num(left)} {num(y_end)} {num(left + radius)} {num(y_end)}"
            f"H{num(right - radius)}Q{num(right)} {num(y_end)} {num(right)} {num(y_end - radius)}V{num(y_base)}Z")


def _frame(chart_id: str, title: str, description: str, width: float, height: float, body: list[str]) -> str:
    return (
        f'<svg class="chart" viewBox="0 0 {num(width)} {num(height)}" role="img" '
        f'aria-labelledby="{esc(chart_id)}-t {esc(chart_id)}-d" focusable="false">'
        f'<title id="{esc(chart_id)}-t">{esc(title)}</title><desc id="{esc(chart_id)}-d">{esc(description)}</desc>'
        + "".join(body) + "</svg>"
    )


def _value_range(series: Sequence[dict[str, Any]], stacked: bool, count: int) -> tuple[Decimal, Decimal]:
    low, high = Decimal(0), Decimal(0)
    for index in range(count):
        if stacked:
            positive = sum((item["values"][index] for item in series if item["values"][index] is not None and item["values"][index] > 0), Decimal(0))
            negative = sum((item["values"][index] for item in series if item["values"][index] is not None and item["values"][index] < 0), Decimal(0))
            high, low = max(high, positive), min(low, negative)
        else:
            for item in series:
                value = item["values"][index]
                if value is not None:
                    high, low = max(high, value), min(low, value)
    return low, high


def bar_chart(chart_id: str, title: str, description: str, categories: Sequence[str], series: Sequence[dict[str, Any]], *,
              horizontal: bool = False, stacked: bool = True, value_text: Formatter, tick_text: Callable[[Decimal], str],
              label_values: bool = True, integer: bool = False) -> str:
    """Vertical or horizontal bars; several series are stacked (default) or grouped side by side."""
    count = len(categories)
    stacked = stacked and len(series) > 1
    low, high = _value_range(series, stacked, count)
    axis_low, axis_high, step = nice_scale(low, high, integer=integer)
    ticks = _ticks(axis_low, axis_high, step)
    tick_labels = [tick_text(tick) for tick in ticks]
    span = float(axis_high - axis_low) or 1.0
    body: list[str] = []
    show_labels = label_values and count <= 12 and (len(series) == 1 or stacked)
    if horizontal:
        label_width = min(200, max(60, 7 * max((len(shorten(label)) for label in categories), default=4)))
        left, right, top = label_width + 12, 56, 8
        row = 28
        plot_width = WIDTH - left - right
        height = top + row * count + 28
        scale = lambda value: left + (float(value - axis_low) / span) * plot_width  # noqa: E731
        base = scale(Decimal(0))
        body.append('<g class="grid">')
        for tick in ticks:
            x = scale(tick)
            body.append(f'<line x1="{num(x)}" y1="{num(top)}" x2="{num(x)}" y2="{num(top + row * count)}"/>')
        body.append("</g>")
        body.append('<g class="axis">')
        for tick, text in zip(ticks, tick_labels):
            body.append(f'<text x="{num(scale(tick))}" y="{num(top + row * count + 18)}" text-anchor="middle">{esc(text)}</text>')
        for index, label in enumerate(categories):
            body.append(f'<text x="{num(left - 8)}" y="{num(top + row * index + row / 2 + 4)}" text-anchor="end">'
                        f'<title>{esc(label)}</title>{esc(shorten(label))}</text>')
        body.append(f'<line class="base" x1="{num(base)}" y1="{num(top)}" x2="{num(base)}" y2="{num(top + row * count)}"/>')
        body.append("</g><g class=\"marks\">")
        for index, label in enumerate(categories):
            band_top = top + row * index
            body.extend(_bars_in_band(series, index, label, band_top, row, scale, base, horizontal=True, stacked=stacked,
                                      value_text=value_text))
            if show_labels:
                total = _band_total(series, index, stacked)
                if total is not None:
                    end = scale(total) if total >= 0 else base
                    body.append(f'<text class="value" x="{num(max(end, base) + 6)}" y="{num(band_top + row / 2 + 4)}">'
                                f'{esc(value_text(total))}</text>')
        body.append("</g>")
        return _frame(chart_id, title, description, WIDTH, height, body)
    rotate = count > 6 or any(len(label) > 12 for label in categories)
    longest = max((len(shorten(label, 18)) for label in categories), default=4)
    bottom = (min(110, 6 * longest + 18) if rotate else 28)
    left = max(44, 7 * max((len(text) for text in tick_labels), default=3) + 12)
    top, right = 18, 12
    plot_width = WIDTH - left - right
    height = top + PLOT_HEIGHT + bottom
    scale = lambda value: top + PLOT_HEIGHT - (float(value - axis_low) / span) * PLOT_HEIGHT  # noqa: E731
    base = scale(Decimal(0))
    band = plot_width / max(count, 1)
    body.append('<g class="grid">')
    for tick in ticks:
        y = scale(tick)
        body.append(f'<line x1="{num(left)}" y1="{num(y)}" x2="{num(WIDTH - right)}" y2="{num(y)}"/>')
    body.append("</g><g class=\"axis\">")
    for tick, text in zip(ticks, tick_labels):
        body.append(f'<text x="{num(left - 8)}" y="{num(scale(tick) + 4)}" text-anchor="end">{esc(text)}</text>')
    every = max(1, (count + 23) // 24)
    for index, label in enumerate(categories):
        if index % every:
            continue
        x = left + band * index + band / 2
        y = top + PLOT_HEIGHT + 16
        if rotate:
            body.append(f'<text x="{num(x)}" y="{num(y)}" text-anchor="end" transform="rotate(-35 {num(x)} {num(y)})">'
                        f'<title>{esc(label)}</title>{esc(shorten(label, 18))}</text>')
        else:
            body.append(f'<text x="{num(x)}" y="{num(y)}" text-anchor="middle"><title>{esc(label)}</title>{esc(shorten(label, 14))}</text>')
    body.append(f'<line class="base" x1="{num(left)}" y1="{num(base)}" x2="{num(WIDTH - right)}" y2="{num(base)}"/>')
    body.append("</g><g class=\"marks\">")
    for index, label in enumerate(categories):
        band_left = left + band * index
        body.extend(_bars_in_band(series, index, label, band_left, band, scale, base, horizontal=False, stacked=stacked,
                                  value_text=value_text))
        if show_labels:
            total = _band_total(series, index, stacked)
            if total is not None:
                end = scale(total) if total >= 0 else base
                body.append(f'<text class="value" x="{num(band_left + band / 2)}" y="{num(min(end, base) - 6)}" '
                            f'text-anchor="middle">{esc(value_text(total))}</text>')
    body.append("</g>")
    return _frame(chart_id, title, description, WIDTH, height, body)


def _band_total(series: Sequence[dict[str, Any]], index: int, stacked: bool) -> Optional[Decimal]:
    values = [item["values"][index] for item in series if item["values"][index] is not None]
    if not values:
        return None
    return sum(values, Decimal(0)) if stacked else values[0]


def _bars_in_band(series: Sequence[dict[str, Any]], index: int, label: str, band_start: float, band: float, scale,
                  base: float, *, horizontal: bool, stacked: bool, value_text: Formatter) -> list[str]:
    marks = []
    if stacked:
        thickness = min(MAX_THICKNESS, band * 0.6)
        offset = band_start + (band - thickness) / 2
        positive, negative = Decimal(0), Decimal(0)
        drawn = [item for item in series if item["values"][index] not in (None, Decimal(0))]
        last_positive = max((n for n, item in enumerate(drawn) if item["values"][index] > 0), default=-1)
        last_negative = max((n for n, item in enumerate(drawn) if item["values"][index] < 0), default=-1)
        for number, item in enumerate(drawn):
            value = item["values"][index]
            if value > 0:
                start, positive = positive, positive + value
                end = positive
                rounded = number == last_positive
            else:
                start, negative = negative, negative + value
                end = negative
                rounded = number == last_negative
            p_start, p_end = scale(start), scale(end)
            gap = 2.0 if start != 0 else 0.0
            if horizontal:
                p_start = p_start + (gap if value > 0 else -gap)
                if (value > 0 and p_end - p_start < 1) or (value < 0 and p_start - p_end < 1):
                    p_start = scale(start)
            else:
                p_start = p_start - (gap if value > 0 else -gap)
                if (value > 0 and p_start - p_end < 1) or (value < 0 and p_end - p_start < 1):
                    p_start = scale(start)
            tip = f"{label} · {item['label']}: {value_text(value)}" if item.get("label") else f"{label}: {value_text(value)}"
            path = _bar_path(offset, p_start, p_end, thickness, horizontal=horizontal, rounded=rounded)
            marks.append(f'<path class="{slot_class(item.get("slot"))}" d="{path}" data-tip="{esc(tip)}"><title>{esc(tip)}</title></path>')
        return marks
    visible = list(series)
    inner = band * (0.6 if len(visible) == 1 else 0.8)
    thickness = min(MAX_THICKNESS, max(2.0, (inner - 2 * (len(visible) - 1)) / max(len(visible), 1)))
    group_width = thickness * len(visible) + 2 * (len(visible) - 1)
    offset = band_start + (band - group_width) / 2
    for number, item in enumerate(visible):
        value = item["values"][index]
        if value is None:
            continue
        x = offset + number * (thickness + 2)
        tip = f"{label} · {item['label']}: {value_text(value)}" if len(visible) > 1 and item.get("label") else f"{label}: {value_text(value)}"
        if value == 0:
            continue
        path = _bar_path(x, base, scale(value), thickness, horizontal=horizontal, rounded=True)
        marks.append(f'<path class="{slot_class(item.get("slot"))}" d="{path}" data-tip="{esc(tip)}"><title>{esc(tip)}</title></path>')
    return marks


def line_chart(chart_id: str, title: str, description: str, categories: Sequence[str], series: Sequence[dict[str, Any]], *,
               value_text: Formatter, tick_text: Callable[[Decimal], str], integer: bool = False) -> str:
    """One 2px line per series over the categories; gaps where a value is missing; ringed markers."""
    count = len(categories)
    low, high = _value_range(series, False, count)
    axis_low, axis_high, step = nice_scale(low, high, integer=integer)
    ticks = _ticks(axis_low, axis_high, step)
    tick_labels = [tick_text(tick) for tick in ticks]
    span = float(axis_high - axis_low) or 1.0
    rotate = count > 8 or any(len(label) > 10 for label in categories)
    longest = max((len(shorten(label, 18)) for label in categories), default=4)
    bottom = min(110, 6 * longest + 18) if rotate else 28
    left = max(44, 7 * max((len(text) for text in tick_labels), default=3) + 12)
    # Converging lines would stack their end labels; several series rely on the legend and tooltips instead.
    end_labels = []
    if len(series) == 1:
        for item in series:
            last = next((value for value in reversed(item["values"]) if value is not None), None)
            if last is not None:
                end_labels.append(value_text(last))
    top, right = 18, max(16, min(200, 7 * max((len(text) for text in end_labels), default=0) + 14))
    plot_width = WIDTH - left - right
    height = top + PLOT_HEIGHT + bottom
    scale = lambda value: top + PLOT_HEIGHT - (float(value - axis_low) / span) * PLOT_HEIGHT  # noqa: E731
    xs = [left + (plot_width * index / (count - 1) if count > 1 else plot_width / 2) for index in range(count)]
    body = ['<g class="grid">']
    for tick in ticks:
        y = scale(tick)
        body.append(f'<line x1="{num(left)}" y1="{num(y)}" x2="{num(left + plot_width)}" y2="{num(y)}"/>')
    body.append('</g><g class="axis">')
    for tick, text in zip(ticks, tick_labels):
        body.append(f'<text x="{num(left - 8)}" y="{num(scale(tick) + 4)}" text-anchor="end">{esc(text)}</text>')
    every = max(1, (count + 15) // 16)
    for index, label in enumerate(categories):
        if index % every and index != count - 1:
            continue
        x, y = xs[index], top + PLOT_HEIGHT + 16
        if rotate:
            body.append(f'<text x="{num(x)}" y="{num(y)}" text-anchor="end" transform="rotate(-35 {num(x)} {num(y)})">'
                        f'<title>{esc(label)}</title>{esc(shorten(label, 18))}</text>')
        else:
            body.append(f'<text x="{num(x)}" y="{num(y)}" text-anchor="middle"><title>{esc(label)}</title>{esc(shorten(label, 12))}</text>')
    body.append(f'<line class="base" x1="{num(left)}" y1="{num(scale(Decimal(0)))}" x2="{num(left + plot_width)}" y2="{num(scale(Decimal(0)))}"/>')
    body.append('</g><g class="marks">')
    for item in series:
        segments, current = [], []
        for index, value in enumerate(item["values"]):
            if value is None:
                if current:
                    segments.append(current)
                current = []
                continue
            current.append((xs[index], scale(value)))
        if current:
            segments.append(current)
        path = "".join("M" + "L".join(f"{num(x)} {num(y)}" for x, y in segment) for segment in segments)
        if path:
            body.append(f'<path class="line {slot_class(item.get("slot"), "s")}" d="{path}"/>')
        markers = count <= 40
        for index, value in enumerate(item["values"]):
            if value is None:
                continue
            tip = f"{categories[index]}" + (f" · {item['label']}" if len(series) > 1 and item.get("label") else "") + f": {value_text(value)}"
            radius = 4 if markers else 2.5
            body.append(f'<circle class="dot {slot_class(item.get("slot"))}" cx="{num(xs[index])}" cy="{num(scale(value))}" r="{radius}" '
                        f'data-tip="{esc(tip)}"><title>{esc(tip)}</title></circle>')
        if len(series) == 1:
            last = next((index for index in range(count - 1, -1, -1) if item["values"][index] is not None), None)
            if last is not None:
                body.append(f'<text class="value" x="{num(xs[last] + 8)}" y="{num(scale(item["values"][last]) + 4)}">'
                            f'{esc(value_text(item["values"][last]))}</text>')
    body.append("</g>")
    return _frame(chart_id, title, description, WIDTH, height, body)


def pie_chart(chart_id: str, title: str, description: str, slices: Sequence[dict[str, Any]], *, donut: bool = True,
              center_text: str = "", center_label: str = "", value_text: Formatter) -> str:
    """Pie or donut; slices are separated by a 2px surface gap; the donut shows the total in its centre."""
    import math

    size = 260
    cx = cy = size / 2
    radius = 116
    inner = radius * 0.6 if donut else 0
    total = sum((item["value"] for item in slices if item["value"] is not None and item["value"] > 0), Decimal(0))
    body = []
    angle = -math.pi / 2
    if total > 0:
        for item in slices:
            value = item["value"]
            if value is None or value <= 0:
                continue
            share = float(value / total)
            tip = f"{item['label']}: {value_text(value)} ({item.get('percent', '')})".replace(" ()", "")
            css = slot_class(item.get("slot"))
            if share >= 0.9999:
                body.append(f'<circle class="{css} slice" cx="{num(cx)}" cy="{num(cy)}" r="{radius}" data-tip="{esc(tip)}"><title>{esc(tip)}</title></circle>')
                angle += 2 * math.pi
                continue
            end = angle + share * 2 * math.pi
            large = 1 if share > 0.5 else 0
            x1, y1 = cx + radius * math.cos(angle), cy + radius * math.sin(angle)
            x2, y2 = cx + radius * math.cos(end), cy + radius * math.sin(end)
            path = f"M{num(cx)} {num(cy)}L{num(x1)} {num(y1)}A{radius} {radius} 0 {large} 1 {num(x2)} {num(y2)}Z"
            body.append(f'<path class="{css} slice" d="{path}" data-tip="{esc(tip)}"><title>{esc(tip)}</title></path>')
            angle = end
    else:
        body.append(f'<circle class="empty" cx="{num(cx)}" cy="{num(cy)}" r="{radius}"/>')
    if donut:
        body.append(f'<circle class="hole" cx="{num(cx)}" cy="{num(cy)}" r="{num(inner)}"/>')
        if center_text:
            body.append(f'<text class="center" x="{num(cx)}" y="{num(cy + 2)}" text-anchor="middle">{esc(center_text)}</text>')
        if center_label:
            body.append(f'<text class="center-label" x="{num(cx)}" y="{num(cy + 22)}" text-anchor="middle">{esc(center_label)}</text>')
    return _frame(chart_id, title, description, size, size, body)


def legend(items: Sequence[tuple[str, Any]], mark: str = "rect") -> str:
    """HTML legend: a coloured key beside each label; the label text keeps the text colour."""
    rows = []
    for label, slot in items:
        key = (f'<svg class="key" viewBox="0 0 16 10" aria-hidden="true"><line class="{slot_class(slot, "s")}" x1="1" y1="5" x2="15" y2="5"/></svg>'
               if mark == "line" else
               f'<svg class="key" viewBox="0 0 10 10" aria-hidden="true"><rect class="{slot_class(slot)}" width="10" height="10" rx="2"/></svg>')
        rows.append(f"<li>{key}<span>{esc(label)}</span></li>")
    return f'<ul class="legend">{"".join(rows)}</ul>'
