// Apply the tape scope passed by the control room once inventory is available.
// Leave all existing review behavior and persistence in the listening desk.
(() => {
  const tape = new URLSearchParams(location.search).get('pipeline_tape');
  if (!/^A15_T\d+$/.test(tape || '')) return;
  const container = document.getElementById('tape-options');
  const observer = new MutationObserver(() => {
    const checks = [...container.querySelectorAll('input[data-tape]')];
    if (!checks.some(input => input.dataset.tape === tape)) return;
    checks.forEach(input => { input.checked = input.dataset.tape === tape; });
    document.getElementById('estimate').click();
    observer.disconnect();
  });
  observer.observe(container, {childList:true,subtree:true});
})();
