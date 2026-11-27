const connectionAlert = document.getElementById("connection-error");
if (connectionAlert) {
    for (const event of ["htmx:sendError", "htmx:responseError", "htmx:timeout"]) {
        document.body.addEventListener(event, () => { connectionAlert.hidden = false; });
    }
    document.body.addEventListener("htmx:afterRequest", ({ detail }) => {
        if (detail.successful && detail.target.id === "controller-cards") {
            connectionAlert.hidden = true;
            htmx.trigger("#monitoring", "refresh-monitoring");
        }
    });
}

const chartTime = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Detroit", month: "short", day: "numeric", year: "numeric",
    hour: "numeric", minute: "2-digit", timeZoneName: "short",
});

function closestChartPoint(points, stamp) {
    let left = 0;
    let right = points.length;
    while (left < right) {
        const mid = Math.floor((left + right) / 2);
        if (points[mid].ts < stamp) left = mid + 1;
        else right = mid;
    }
    const after = points[left];
    const before = points[left - 1];
    if (before && (!after || stamp - before.ts <= after.ts - stamp)) return before;
    return after;
}

function setupChart(viewport) {
    if (viewport.dataset.initialized) return;
    viewport.dataset.initialized = "true";

    const points = JSON.parse(viewport.dataset.points);
    const series = JSON.parse(viewport.dataset.series).filter(([key]) => points[key]?.length);
    const unit = viewport.dataset.unit;
    const precision = Number(viewport.dataset.precision);
    const timeline = Array.from(new Map(
        Object.values(points).flat().map(point => [point.ts, point])
    ).values()).sort((a, b) => a.ts - b.ts);
    const tooltip = viewport.querySelector(".chart-readout");
    const cursor = viewport.querySelector(".chart-cursor");
    const cursorLine = cursor.querySelector("line");
    const markers = Array.from(cursor.querySelectorAll("[data-marker]"));
    const start = Number(viewport.dataset.start);
    const end = Number(viewport.dataset.end);
    const intervalChart = timeline[0]?.start != null;
    const maxDistance = 6 * 60;
    let selected = null;

    function hide() {
        selected = null;
        tooltip.hidden = true;
        cursor.setAttribute("visibility", "hidden");
    }

    function show(point) {
        if (!point) return hide();
        if (selected === point) return;
        selected = point;
        const stamp = new Date(point.ts * 1000);
        tooltip.querySelector("[data-readout-time]").textContent = intervalChart
            ? chartTime.formatRange(new Date(point.start * 1000), new Date(point.end * 1000))
            : chartTime.format(stamp);
        tooltip.classList.toggle("readout-right", point.x < 368);
        cursorLine.setAttribute("x1", point.x);
        cursorLine.setAttribute("x2", point.x);

        const values = tooltip.querySelector("dl");
        values.replaceChildren();
        for (const [key, label] of series) {
            const item = closestChartPoint(points[key] || [], point.ts);
            const present = item && (intervalChart ? item.ts === point.ts : Math.abs(item.ts - point.ts) <= maxDistance);
            const name = document.createElement("dt");
            name.textContent = label;
            const value = document.createElement("dd");
            value.textContent = present ? `${item.value.toLocaleString("en-US", {
                maximumFractionDigits: precision,
                minimumFractionDigits: key === "avoided" ? 2 : 0,
            })} ${unit}` : "—";
            if (present && Math.floor(item.ts / 60) !== Math.floor(point.ts / 60)) {
                const time = document.createElement("time");
                time.textContent = chartTime.format(new Date(item.ts * 1000));
                value.append(time);
            }
            values.append(name, value);
            const marker = markers.find(node => node.dataset.marker === key);
            marker.setAttribute("visibility", present ? "visible" : "hidden");
            if (present) {
                marker.setAttribute("cx", item.x);
                marker.setAttribute("cy", item.y);
            }
        }
        tooltip.hidden = false;
        cursor.setAttribute("visibility", "visible");
    }

    function move(event) {
        const rect = viewport.getBoundingClientRect();
        if (!rect.width) return;
        const stamp = start + (end - start) * (event.clientX - rect.left) / rect.width;
        const point = intervalChart
            ? timeline.find(item => stamp >= item.start && stamp <= item.end)
            : closestChartPoint(timeline, stamp);
        show(point && (intervalChart || Math.abs(point.ts - stamp) <= maxDistance) ? point : null);
    }

    viewport.addEventListener("pointermove", move);
    viewport.addEventListener("pointerdown", move);
    viewport.addEventListener("pointerleave", event => { if (event.pointerType === "mouse") hide(); });
    viewport.addEventListener("blur", hide);
    viewport.addEventListener("keydown", event => {
        if (event.key === "Escape") hide();
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
        event.preventDefault();
        const index = selected ? timeline.indexOf(selected) + (event.key === "ArrowLeft" ? -1 : 1) : timeline.length - 1;
        show(timeline[Math.max(0, Math.min(timeline.length - 1, index))]);
    });
}

function setupCharts() {
    document.querySelectorAll(".chart-viewport").forEach(setupChart);
}

setupCharts();
document.body.addEventListener("htmx:afterSwap", setupCharts);
