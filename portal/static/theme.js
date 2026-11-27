(() => {
    const key = "campus-farm-theme";
    const systemTheme = matchMedia("(prefers-color-scheme: dark)");
    let preference = "auto";
    try {
        const saved = localStorage.getItem(key);
        if (saved === "light" || saved === "dark") preference = saved;
    } catch {
        // Blocked storage still allows theme changes for this page.
    }

    function applyTheme() {
        const system = systemTheme.matches ? "dark" : "light";
        document.documentElement.dataset.theme = preference === "auto" ? system : preference;
    }

    applyTheme();
    systemTheme.addEventListener("change", applyTheme);
    document.addEventListener("DOMContentLoaded", () => {
        const select = document.getElementById("portal-theme");
        select.value = preference;
        document.getElementById("theme-picker").hidden = false;
        select.addEventListener("change", () => {
            preference = select.value;
            applyTheme();
            try {
                localStorage.setItem(key, preference);
            } catch {
                // The selected theme is already applied.
            }
        });
    });
})();
