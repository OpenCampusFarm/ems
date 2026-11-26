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
