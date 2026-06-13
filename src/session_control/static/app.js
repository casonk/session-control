document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy]");
  if (!button) {
    return;
  }
  const value = button.getAttribute("data-copy") || "";
  try {
    await navigator.clipboard.writeText(value);
    const original = button.textContent;
    button.textContent = "Copied";
    window.setTimeout(() => {
      button.textContent = original;
    }, 1200);
  } catch {
    window.prompt("Copy command", value);
  }
});

const selectedSessionInputs = () => Array.from(document.querySelectorAll("[data-session-select]"));
const checkedSessionInputs = () => selectedSessionInputs().filter((input) => input.checked);

function syncBulkControls() {
  const selected = checkedSessionInputs();
  const actions = document.querySelectorAll("[data-selected-action]");
  actions.forEach((button) => {
    button.disabled = selected.length === 0;
  });

  const hasSelection = selected.length > 0;
  document.querySelector(".bulk-controls")?.classList.toggle("floating", hasSelection);
  document.querySelector(".shell")?.classList.toggle("has-selection", hasSelection);

  const selectAll = document.querySelector("[data-select-all]");
  if (!selectAll) {
    return;
  }
  const all = selectedSessionInputs();
  selectAll.checked = all.length > 0 && selected.length === all.length;
  selectAll.indeterminate = selected.length > 0 && selected.length < all.length;
}

document.addEventListener("change", (event) => {
  const selectAll = event.target.closest("[data-select-all]");
  if (selectAll) {
    selectedSessionInputs().forEach((input) => {
      input.checked = selectAll.checked;
    });
    syncBulkControls();
    return;
  }

  if (event.target.closest("[data-session-select]")) {
    syncBulkControls();
  }
});

document.addEventListener("submit", (event) => {
  const form = event.target.closest("[data-bulk-form]");
  if (!form) {
    return;
  }
  form.querySelectorAll("input[name='session_id']").forEach((input) => input.remove());
  checkedSessionInputs().forEach((input) => {
    const hidden = document.createElement("input");
    hidden.type = "hidden";
    hidden.name = "session_id";
    hidden.value = input.value;
    form.appendChild(hidden);
  });
});

syncBulkControls();
