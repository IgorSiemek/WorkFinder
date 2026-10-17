document.querySelectorAll('.search-form').forEach((form) => {
  form.addEventListener('submit', () => {
    const button = form.querySelector('button');
    button.disabled = true;
    button.innerHTML = '<span class="button-icon spinning">↻</span> Searching…';
  });
});

let dragged = null;
document.querySelectorAll('.kanban-card').forEach((card) => {
  card.addEventListener('dragstart', () => {
    dragged = card;
    card.classList.add('dragging');
  });
  card.addEventListener('dragend', () => card.classList.remove('dragging'));
});

const statusMap = {
  Saved: 'Saved', Preparing: 'Preparing', Applied: 'Applied', Interview: 'Recruiter Screen',
  Offer: 'Offer', Rejected: 'Rejected'
};
document.querySelectorAll('.kanban-drop').forEach((zone) => {
  zone.addEventListener('dragover', (event) => {
    event.preventDefault();
    zone.classList.add('drag-over');
  });
  zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
  zone.addEventListener('drop', async (event) => {
    event.preventDefault();
    zone.classList.remove('drag-over');
    if (!dragged) return;
    zone.appendChild(dragged);
    const form = new FormData();
    form.append('status', statusMap[zone.dataset.status]);
    try {
      const response = await fetch(`/actions/applications/${dragged.dataset.id}/status`, {
        method: 'POST', body: form
      });
      if (!response.ok) throw new Error('Could not save application status');
      window.location.assign(response.url);
    } catch {
      window.location.reload();
    }
  });
});
