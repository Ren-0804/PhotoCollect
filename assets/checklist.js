(() => {
  const list = document.querySelector("[data-checklist]");
  if (!list) return;

  const key = list.dataset.storageKey || "lesson-checklist";
  const boxes = [...list.querySelectorAll('input[type="checkbox"]')];
  const bar = document.querySelector("[data-progress-bar]");
  const label = document.querySelector("[data-progress-label]");
  const success = document.querySelector("[data-complete-message]");

  try {
    const saved = JSON.parse(localStorage.getItem(key) || "[]");
    boxes.forEach((box, index) => {
      box.checked = Boolean(saved[index]);
    });
  } catch (_) {
    // The lesson still works when local storage is unavailable.
  }

  function update() {
    const completed = boxes.filter((box) => box.checked).length;
    const percent = boxes.length ? Math.round((completed / boxes.length) * 100) : 0;
    if (bar) bar.style.width = `${percent}%`;
    if (label) label.textContent = `${completed}/${boxes.length} 项已验证`;
    if (success) success.hidden = completed !== boxes.length;
    try {
      localStorage.setItem(key, JSON.stringify(boxes.map((box) => box.checked)));
    } catch (_) {
      // Ignore storage failures.
    }
  }

  boxes.forEach((box) => box.addEventListener("change", update));
  update();
})();
