// The setup page: the only page with access to the app's commands (see capabilities).
const { invoke } = window.__TAURI__.core;

const choices = { project: null, python: null };
const $ = (id) => document.getElementById(id);

function show(id, visible) {
  $(id).hidden = !visible;
}

function render() {
  $('project').textContent = choices.project ?? 'No folder chosen';
  $('python').textContent = choices.python ?? 'Not chosen';
  $('start').disabled = !choices.project;
}

async function choose(command, key) {
  $('choice-error').textContent = '';
  try {
    const picked = await invoke(command);
    if (picked) choices[key] = picked;
  } catch (error) {
    $('choice-error').textContent = String(error);
  }
  render();
}

async function start() {
  show('failed', false);
  show('choices', false);
  show('starting', true);
  try {
    await invoke('start', { project: choices.project, python: choices.python });
    // The window now shows the workspace.
  } catch (error) {
    $('failure').textContent = String(error);
    show('starting', false);
    show('failed', true);
    show('choices', true);
    $('start').focus();
  }
}

$('choose-project').addEventListener('click', () => choose('choose_project', 'project'));
$('choose-python').addEventListener('click', () => choose('choose_python', 'python'));
$('choices').addEventListener('submit', (event) => {
  event.preventDefault();
  if (choices.project) start();
});

const saved = await invoke('current_choices');
choices.project = saved.project ?? null;
choices.python = saved.python ?? null;
render();
if (saved.valid) start();
