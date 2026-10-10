'use strict';
document.getElementById('login-form').onsubmit = async event => {
  event.preventDefault(); const button = event.submitter, error = document.getElementById('login-error'); button.disabled = true;
  try {
    const response = await fetch('/login/session', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: document.getElementById('password').value }) });
    const data = await response.json(); if (!response.ok) throw new Error(data.detail || 'Unable to sign in.');
    location.assign('/admin');
  } catch (failure) { error.textContent = failure.message; error.hidden = false; }
  finally { button.disabled = false; }
};
