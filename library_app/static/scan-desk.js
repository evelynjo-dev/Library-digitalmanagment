(() => {
  const desk = document.getElementById('scan-desk');
  if (!desk) return;

  const urls = {
    lookup: desk.dataset.lookupUrl,
    issue: desk.dataset.issueUrl,
    return: desk.dataset.returnUrl
  };
  const instructions = document.getElementById('scan-instructions');
  const message = document.getElementById('scan-message');
  const studentCard = document.getElementById('student-card');
  const copyCard = document.getElementById('copy-card');
  const confirmation = document.getElementById('scan-confirmation');
  const issueForm = document.getElementById('manual-issue-form');
  const returnForm = document.getElementById('manual-return-form');
  const cameraStatus = document.getElementById('camera-status');
  const startButton = document.getElementById('camera-start');
  const stopButton = document.getElementById('camera-stop');
  let mode = 'issue';
  let student = null;
  let scanner = null;
  let cameraStarted = false;
  let processing = false;
  let lastDecoded = '';

  function setMessage(text, kind) {
    message.textContent = text;
    message.className = `scan-message${kind ? ` ${kind}` : ''}`;
  }

  function setText(id, value) {
    document.getElementById(id).textContent = value == null ? '' : String(value);
  }

  function showStudent(person) {
    student = person;
    studentCard.hidden = false;
    setText('student-name', person.name);
    setText('student-member-id', person.member_id);
    setText('student-books-held', `${person.books_held} / ${person.borrowing_limit} books`);
    const eligibility = document.getElementById('student-eligibility');
    eligibility.textContent = person.eligible
      ? 'Eligible to borrow'
      : `Borrowing limit reached (${person.borrowing_limit} books).`;
    eligibility.className = `scan-eligibility${person.eligible ? ' eligible' : ' ineligible'}`;
    document.getElementById('change-student').hidden = mode !== 'issue';
  }

  function showCopy(bookCopy) {
    copyCard.hidden = false;
    setText('copy-title', bookCopy.title);
    setText('copy-author', bookCopy.author);
    setText('copy-id', bookCopy.copy_id);
    setText('copy-location', `${bookCopy.category} | Rack ${bookCopy.rack}, shelf ${bookCopy.shelf}`);
  }

  function clearCopy() {
    copyCard.hidden = true;
    confirmation.hidden = true;
  }

  function showConfirmation(result, action) {
    confirmation.hidden = false;
    setText('confirmation-title', action === 'issue' ? 'Issue complete' : 'Return complete');
    setText('confirmation-message', result.message);
    setText(
      'confirmation-due',
      action === 'issue' ? `Due date: ${result.transaction.due_date}` : 'The copy is available again.'
    );
    const fine = document.getElementById('confirmation-fine');
    fine.hidden = !result.fine;
    fine.textContent = result.fine
      ? `Overdue by ${result.late_days} day(s). Fine due: Rs.${result.fine}.`
      : '';
    setMessage(result.message, 'success');
    renderRecent(result.recent_transactions || []);
    showStudent(result.student);
    showCopy(result.copy);
  }

  async function post(url, payload) {
    let response;
    try {
      response = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'application/json',
          'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content
        },
        body: JSON.stringify(payload)
      });
    } catch (error) {
      throw new Error('Could not reach the library server. Check the connection and try again.');
    }

    let data;
    try {
      data = await response.json();
    } catch (error) {
      throw new Error('The library server returned an unreadable response.');
    }
    if (!response.ok) throw new Error(data.error || 'The scan could not be completed.');
    return data;
  }

  async function identifyStudent(code) {
    const result = await post(urls.lookup, { code, mode: 'issue' });
    showStudent(result.student);
    clearCopy();
    if (!result.student.eligible) {
      setMessage(result.message, 'error');
      return false;
    }
    setMessage(`${result.student.name} identified. Scan the book copy.`, 'success');
    return true;
  }

  async function issueScannedCopy(code) {
    if (!student) {
      await identifyStudent(code);
      return;
    }
    if (!student.eligible) {
      setMessage(`${student.name} has reached the borrowing limit. Choose another student.`, 'error');
      return;
    }
    const preview = await post(urls.lookup, {
      code,
      mode: 'issue',
      member_id: student.member_id
    });
    showStudent(preview.student);
    showCopy(preview.copy);
    setMessage(preview.message, 'neutral');
    const result = await post(urls.issue, {
      member_id: preview.student.member_id,
      copy_id: preview.copy.copy_id
    });
    showConfirmation(result, 'issue');
  }

  async function returnScannedCopy(code) {
    const preview = await post(urls.lookup, { code, mode: 'return' });
    showStudent(preview.student);
    showCopy(preview.copy);
    setMessage(preview.message, 'neutral');
    const result = await post(urls.return, { copy_id: preview.copy.copy_id });
    showConfirmation(result, 'return');
  }

  async function handleCode(code) {
    const value = code.trim();
    if (!value) return;
    clearCopy();
    setMessage('Checking the scanned code against the library database...', 'neutral');
    if (mode === 'issue') {
      await issueScannedCopy(value);
    } else {
      await returnScannedCopy(value);
    }
  }

  function renderRecent(rows) {
    const body = document.getElementById('scan-recent-body');
    body.replaceChildren();
    if (!rows.length) {
      const row = document.createElement('tr');
      row.className = 'scan-empty-row';
      const cell = document.createElement('td');
      cell.colSpan = 6;
      cell.textContent = 'No transactions recorded yet.';
      row.appendChild(cell);
      body.appendChild(row);
      return;
    }
    rows.forEach((item) => {
      const row = document.createElement('tr');
      [item.title, item.copy_id, item.member_name, item.issue_date, item.due_date]
        .forEach((value, index) => {
          const cell = document.createElement('td');
          cell.textContent = value || '';
          if (index === 1) {
            const code = document.createElement('code');
            code.textContent = value || '';
            cell.replaceChildren(code);
          }
          row.appendChild(cell);
        });
      const status = document.createElement('td');
      const badge = document.createElement('span');
      badge.className = `badge ${item.return_date ? 'ok' : 'low'}`;
      badge.textContent = item.return_date ? 'Returned' : 'Issued';
      status.appendChild(badge);
      row.appendChild(status);
      body.appendChild(row);
    });
  }

  async function submitIssue(event) {
    event.preventDefault();
    const memberCode = document.getElementById('manual-member-id').value.trim();
    const copyCode = document.getElementById('manual-issue-copy-id').value.trim();
    const submit = issueForm.querySelector('button[type="submit"]');
    submit.disabled = true;
    try {
      if (await identifyStudent(memberCode)) await issueScannedCopy(copyCode);
    } catch (error) {
      setMessage(error.message, 'error');
    } finally {
      submit.disabled = false;
    }
  }

  async function submitReturn(event) {
    event.preventDefault();
    const copyCode = document.getElementById('manual-return-copy-id').value.trim();
    const submit = returnForm.querySelector('button[type="submit"]');
    submit.disabled = true;
    try {
      await returnScannedCopy(copyCode);
    } catch (error) {
      setMessage(error.message, 'error');
    } finally {
      submit.disabled = false;
    }
  }

  document.querySelectorAll('.scan-mode').forEach((button) => {
    button.addEventListener('click', () => {
      mode = button.dataset.mode;
      student = null;
      lastDecoded = '';
      studentCard.hidden = true;
      clearCopy();
      document.querySelectorAll('.scan-mode').forEach((item) => {
        const selected = item === button;
        item.classList.toggle('active', selected);
        item.setAttribute('aria-pressed', String(selected));
      });
      issueForm.hidden = mode !== 'issue';
      returnForm.hidden = mode !== 'return';
      instructions.textContent = mode === 'issue'
        ? "Scan a student's digital library card first, then scan the book copy."
        : 'Scan the QR code on a currently issued book copy to return it.';
      setMessage(mode === 'issue' ? 'Ready to identify a student.' : 'Ready to scan a book copy.');
    });
  });

  document.getElementById('change-student').addEventListener('click', () => {
    student = null;
    lastDecoded = '';
    studentCard.hidden = true;
    clearCopy();
    setMessage('Ready to identify a different student.');
  });
  issueForm.addEventListener('submit', submitIssue);
  returnForm.addEventListener('submit', submitReturn);

  startButton.addEventListener('click', async () => {
    if (!window.Html5Qrcode) {
      cameraStatus.textContent = 'The QR scanner library did not load. Use the manual ID fields below.';
      cameraStatus.className = 'camera-status error';
      return;
    }
    try {
      if (!scanner) scanner = new Html5Qrcode('qr-reader');
      await scanner.start(
        { facingMode: 'environment' },
        { fps: 10, qrbox: { width: 240, height: 240 } },
        async (decodedText) => {
          if (processing || decodedText === lastDecoded) return;
          processing = true;
          lastDecoded = decodedText;
          try {
            await handleCode(decodedText);
          } catch (error) {
            setMessage(error.message, 'error');
          } finally {
            processing = false;
          }
        },
        () => {}
      );
      cameraStarted = true;
      startButton.disabled = true;
      stopButton.disabled = false;
      cameraStatus.textContent = 'Camera is active. Point it at a student card or book QR code.';
      cameraStatus.className = 'camera-status active';
    } catch (error) {
      cameraStatus.textContent =
        'Camera access failed. Allow camera permission and use a secure localhost connection, or enter IDs manually.';
      cameraStatus.className = 'camera-status error';
    }
  });

  stopButton.addEventListener('click', async () => {
    if (!scanner || !cameraStarted) return;
    try {
      await scanner.stop();
      cameraStarted = false;
      startButton.disabled = false;
      stopButton.disabled = true;
      cameraStatus.textContent = 'Camera is off. Start it to scan a QR code.';
      cameraStatus.className = 'camera-status';
    } catch (error) {
      cameraStatus.textContent = 'Could not stop the camera cleanly. Reload the page if it remains active.';
      cameraStatus.className = 'camera-status error';
    }
  });

  window.addEventListener('pagehide', () => {
    if (scanner && cameraStarted) {
      scanner.stop().catch((error) => console.error('Could not stop the QR scanner:', error));
    }
  });
})();
