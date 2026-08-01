function updateReasoning(suffix) {
  suffix = suffix || '';
  var model = document.getElementById('model' + suffix);
  var reasoning = document.getElementById('reasoning' + suffix);
  if (!model || !reasoning) return;
  var selected = model.options[model.selectedIndex];
  var levels = selected ? JSON.parse(selected.dataset.levels || '[]') : [];
  for (var i = 0; i < reasoning.options.length; i++) {
    var ok = levels.indexOf(reasoning.options[i].value) !== -1;
    reasoning.options[i].hidden = !ok;
    reasoning.options[i].disabled = !ok;
  }
  if (levels.indexOf(reasoning.value) === -1) {
    var def = selected ? (selected.dataset.defaultReasoning || 'medium') : 'medium';
    reasoning.value = levels.indexOf(def) !== -1 ? def : (levels[0] || 'medium');
  }
}

function updateModelSummary(suffix) {
  suffix = suffix || '';
  var agent = document.getElementById('agent' + suffix);
  var details = agent && agent.closest('details');
  if (!details) return;
  var summary = details.querySelector('summary');
  if (!summary) return;
  var kimi = agent.value === 'kimi';
  var model = document.getElementById('model' + suffix);
  if (kimi) {
    summary.textContent = (model && model.selectedOptions[0] ? model.selectedOptions[0].textContent : 'Kimi') + ' \u25BE';
  } else {
    var live = model && model.selectedOptions[0] ? model.selectedOptions[0].textContent : '';
    summary.textContent = (summary.dataset.codexLabel || live) + ' \u25BE';
  }
}

function updateAgent(suffix) {
  suffix = suffix || '';
  var agent = document.getElementById('agent' + suffix);
  if (!agent) return;
  var kimi = agent.value === 'kimi';
  var panel = agent.closest('.composer-config-panel') || agent.closest('details');
  if (panel) {
    var model = panel.querySelector('select[name="model"]');
    var reasoningWrap = panel.querySelector('.reasoning-wrap');
    var permission = panel.querySelector('select[name="permission"]');
    if (model) {
      model.disabled = false;
      if (kimi) {
        if (!model.dataset.codexSaved) model.dataset.codexSaved = model.innerHTML;
        model.innerHTML = model.dataset.kimiOptions || '';
        if (!model.querySelector('option[selected]')) {
          var opts = Array.from(model.options).map(function (o) { return o.value; });
          var preferred = model.dataset.kimiDefault || '';
          model.value = opts.indexOf(preferred) !== -1 ? preferred : (opts[0] || '');
        }
      } else {
        if (model.dataset.codexSaved) model.innerHTML = model.dataset.codexSaved;
        updateReasoning(suffix);
      }
    }
    if (reasoningWrap) reasoningWrap.style.display = kimi ? 'none' : '';
    if (permission) permission.disabled = false;
  }
  var details = agent.closest('details');
  if (details) {
    var summary = details.querySelector('summary');
    if (summary) {
      if (kimi) {
        var m = document.getElementById('model' + suffix);
        summary.textContent = (m && m.selectedOptions[0] ? m.selectedOptions[0].textContent : 'Kimi') + ' \u25BE';
      } else {
        var m2 = document.getElementById('model' + suffix);
        var live = m2 && m2.selectedOptions[0] ? m2.selectedOptions[0].textContent : '';
        summary.textContent = (summary.dataset.codexLabel || live) + ' \u25BE';
        updateReasoning(suffix);
      }
    }
  }
}

document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll("select[id^='model']").forEach(function (s) {
    s.addEventListener('change', function () {
      updateModelSummary(s.id.slice(5));
    });
    updateReasoning(s.id.slice(5));
  });
  document.querySelectorAll("select[id^='agent']").forEach(function (s) {
    updateAgent(s.id.slice(5));
  });
});
