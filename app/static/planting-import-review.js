(function () {
  const form = document.getElementById("plant-review-form");
  if (!form) return;

  function syncChoiceStyles(block) {
    block.querySelectorAll(".plant-hyb-choice-btn").forEach((btn) => {
      const input = btn.querySelector('input[type="radio"]');
      btn.classList.toggle("is-selected", !!(input && input.checked));
    });
  }

  function syncHyb(block) {
    const sel = block.querySelector("[data-hyb-sel]");
    const neu = block.querySelector("[data-hyb-new]");
    if (!sel || !neu) return;
    const isNew = sel.value === "new";
    neu.hidden = !isNew;
    if (!isNew) return;
    syncChoiceStyles(block);
    const now = block.querySelector("[data-hyb-detail-now]");
    const fields = block.querySelector("[data-hyb-detail-fields]");
    if (fields) {
      fields.hidden = !(now && now.checked);
    }
  }

  form.querySelectorAll("[data-plant-hyb]").forEach((block) => {
    syncHyb(block);
    const sel = block.querySelector("[data-hyb-sel]");
    if (sel) sel.addEventListener("change", () => syncHyb(block));
    block.querySelectorAll(".plant-hyb-choice-btn input").forEach((r) => {
      r.addEventListener("change", () => syncHyb(block));
    });
  });
})();
