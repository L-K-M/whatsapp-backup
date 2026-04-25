<script>
  import { onDestroy, onMount } from 'svelte';
  import {
    BalloonHelp,
    Button,
    DataTable,
    ErrorBanner,
    ExpandableSection,
    TitleBar
  } from '@lkmc/system7-ui';

  const PAGE_SIZE = 80;
  const POLL_MS = 2500;

  let status = null;
  let chats = [];
  let messages = [];
  let totalMessages = 0;
  let selectedChat = '';
  let selectedMessage = null;
  let chatQuery = '';
  let searchQuery = '';
  let offset = 0;
  let loading = true;
  let messagesLoading = false;
  let actionPending = '';
  let errorMessage = '';
  let exportStatusText = '';
  let pollTimer = null;
  let logPane;
  let detailsExpanded = false;
  let lastIndexedCount = null;
  let lastTerminalLogText = '';
  let terminalScrollFrame = null;

  const ARCHIVE_HELP = 'Plain-text backups are stored under the configured archive directory, visible on the host as DATA_PATH/archive/messages. The count is how many message text files the app has indexed for browsing/search.';
  const SYNC_SAFETY_HELP = 'The app runs only one wacli sync process at a time. If it fails, reconnect attempts use backoff so the wrapper does not repeatedly hammer WhatsApp.';
  const SYNC_STATE_HELP = 'This is a state indicator, not a finite progress percentage. WhatsApp sync is continuous: when active, wacli is connected and watching for new messages/media.';
  const EXPORT_HELP = 'Export Now immediately reads wacli\'s local message cache, writes new or changed plain-text files under the archive directory, and downloads a zip. This also runs automatically in the background.';
  const WACLI_ACTIVITY_HELP = 'Raw wacli output for login, sync, media downloads, and errors. It is useful for troubleshooting; normal browsing/search uses the exported text archive.';

  const chatColumns = [
    { key: 'name', label: 'Chat', width: '52%' },
    { key: 'count', label: 'Msgs', width: '16%' },
    { key: 'last', label: 'Last', width: '32%' }
  ];

  const messageColumns = [
    { key: 'time', label: 'Time', width: '168px' },
    { key: 'sender', label: 'Sender', width: '150px' },
    { key: 'message', label: 'Message' },
    { key: 'media', label: 'Media', width: '88px' }
  ];

  $: authIdentity = status?.auth?.linked_jid || status?.auth?.phone || '';
  $: authenticated = Boolean(status?.auth?.authenticated || authIdentity);
  $: authLines = status?.auth_process?.lines || [];
  $: syncLines = status?.sync_process?.lines || [];
  $: syncRunning = Boolean(status?.sync_process?.running);
  $: authRunning = Boolean(status?.auth_process?.running);
  $: displayLines = authRunning || (!authenticated && authLines.length > 0)
    ? authLines
    : syncLines.length > 0
      ? syncLines
      : authLines;
  $: exportInfo = status?.export || {};
  $: notificationInfo = status?.notifications || {};
  $: smtpInfo = notificationInfo?.smtp || {};
  $: syncControl = status?.sync_control || {};
  $: syncBackoffRemaining = Number(syncControl?.backoff_remaining_seconds || 0);
  $: activityDisplay = splitQrActivity(displayLines);
  $: terminalLogText = buildTerminalLogText(activityDisplay.logLines);
  $: statusLabel = authRunning
    ? 'Waiting for QR scan'
    : !authenticated
      ? 'Not logged in'
      : syncRunning
        ? 'Syncing messages and media'
        : 'Logged in, sync stopped';
  $: notificationLabel = !smtpInfo.enabled
    ? 'SMTP alerts disabled'
    : notificationInfo.outage_active
      ? notificationInfo.last_email_sent_at
        ? `Alert sent ${formatTime(notificationInfo.last_email_sent_at)}`
        : 'Outage alert pending or failed'
      : `SMTP alerts to ${smtpInfo.to}`;
  $: syncMeterLabel = syncRunning
    ? 'Sync active (continuous)'
    : syncBackoffRemaining > 0
      ? `Reconnect backoff: ${syncBackoffRemaining}s`
      : authenticated
        ? 'Connected, waiting for changes'
        : 'Not connected';
  $: syncMeterState = syncRunning
    ? 'active'
    : syncBackoffRemaining > 0
      ? 'backoff'
      : authenticated
        ? 'ready'
        : 'offline';
  $: statusSummary = authenticated
    ? `${statusLabel} | ${authIdentity || 'linked account'} | ${status?.export?.last_indexed_count || 0} text records`
    : statusLabel;
  $: selectedChatName = selectedChat
    ? chats.find((chat) => chat.jid === selectedChat)?.name || selectedChat
    : 'All chats';
  $: pageStart = totalMessages === 0 ? 0 : offset + 1;
  $: pageEnd = Math.min(offset + PAGE_SIZE, totalMessages);
  $: if (terminalLogText !== lastTerminalLogText) {
    lastTerminalLogText = terminalLogText;
    queueTerminalScroll();
  }

  onMount(() => {
    void loadInitial();
    pollTimer = setInterval(() => {
      void refreshStatus(false);
    }, POLL_MS);
  });

  onDestroy(() => {
    if (pollTimer) {
      clearInterval(pollTimer);
    }
    if (terminalScrollFrame !== null) {
      cancelAnimationFrame(terminalScrollFrame);
    }
  });

  async function api(path, options = {}) {
    const response = await fetch(path, options);
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(body.error || `Request failed (${response.status})`);
    }
    return body;
  }

  async function loadInitial() {
    loading = true;
    try {
      await refreshStatus(true);
      await loadChats();
      await loadMessages(true);
    } catch (error) {
      errorMessage = error.message || 'Failed to load app state.';
    } finally {
      loading = false;
    }
  }

  async function refreshStatus(scrollLogs = true) {
    try {
      const nextStatus = await api('/api/status');
      const nextIndexedCount = Number(nextStatus?.export?.last_indexed_count || 0);
      const shouldReloadMessages = lastIndexedCount !== null && nextIndexedCount !== lastIndexedCount;
      status = nextStatus;
      lastIndexedCount = nextIndexedCount;
      if (shouldReloadMessages) {
        await loadChats();
        await loadMessages(false);
      }
      if (scrollLogs) {
        queueTerminalScroll();
      }
    } catch (error) {
      errorMessage = error.message || 'Failed to refresh status.';
    }
  }

  async function loadChats() {
    const params = new URLSearchParams();
    params.set('limit', '500');
    if (chatQuery.trim()) {
      params.set('q', chatQuery.trim());
    }
    const payload = await api(`/api/chats?${params}`);
    chats = payload.chats || [];
  }

  async function loadMessages(reset = false) {
    if (reset) {
      offset = 0;
    }
    messagesLoading = true;
    const params = new URLSearchParams();
    params.set('limit', String(PAGE_SIZE));
    params.set('offset', String(offset));
    if (selectedChat) {
      params.set('chat', selectedChat);
    }
    if (searchQuery.trim()) {
      params.set('q', searchQuery.trim());
    }
    try {
      const payload = await api(`/api/messages?${params}`);
      messages = payload.messages || [];
      totalMessages = payload.total || 0;
      if (!selectedMessage || !messages.some((msg) => msg.file === selectedMessage.file)) {
        selectedMessage = messages[0] || null;
      }
    } catch (error) {
      errorMessage = error.message || 'Failed to load messages.';
    } finally {
      messagesLoading = false;
    }
  }

  async function runAction(name, callback) {
    if (actionPending) {
      return;
    }
    actionPending = name;
    errorMessage = '';
    try {
      await callback();
      await refreshStatus(true);
      await loadChats();
      await loadMessages(false);
    } catch (error) {
      errorMessage = error.message || `${name} failed.`;
    } finally {
      actionPending = '';
    }
  }

  function startLogin() {
    detailsExpanded = true;
    void runAction('login', async () => {
      await api('/api/auth/start', { method: 'POST' });
    });
  }

  function logout() {
    void runAction('logout', async () => {
      await api('/api/auth/logout', { method: 'POST' });
    });
  }

  function startSync() {
    void runAction('sync', async () => {
      await api('/api/sync/start', { method: 'POST' });
    });
  }

  function runExport() {
    if (actionPending) {
      return;
    }
    actionPending = 'export';
    errorMessage = '';
    exportStatusText = 'Exporting synced messages and preparing download...';
    void (async () => {
      try {
        const payload = await downloadExportArchive();
        exportStatusText = payload.fileCount === 0
          ? 'Export completed; downloaded an empty archive because no text files exist yet.'
          : `Downloaded ${payload.fileCount} archived files (${payload.exportedCount} new or changed).`;
        await refreshStatus(true);
        await loadChats();
        await loadMessages(false);
      } catch (error) {
        exportStatusText = '';
        errorMessage = error.message || 'Export failed.';
      } finally {
        actionPending = '';
      }
    })();
  }

  async function downloadExportArchive() {
    const response = await fetch('/api/export/download', { method: 'POST' });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.error || `Export failed (${response.status})`);
    }

    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = downloadFilename(response.headers.get('content-disposition')) || fallbackExportFilename();
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);

    return {
      exportedCount: Number(response.headers.get('x-exported-count') || 0),
      fileCount: Number(response.headers.get('x-archive-file-count') || 0)
    };
  }

  function downloadFilename(contentDisposition) {
    const header = String(contentDisposition || '');
    const encodedMatch = header.match(/filename\*=UTF-8''([^;]+)/i);
    if (encodedMatch) {
      return decodeURIComponent(encodedMatch[1].replace(/"/g, ''));
    }
    const match = header.match(/filename="?([^";]+)"?/i);
    return match ? match[1] : '';
  }

  function fallbackExportFilename() {
    return `whatsapp-backup-export-${new Date().toISOString().replace(/[:.]/g, '-')}.zip`;
  }

  function handleDetailsExpanded(expanded) {
    detailsExpanded = expanded;
    queueTerminalScroll();
  }

  function selectChat(jid) {
    selectedChat = jid;
    selectedMessage = null;
    void loadMessages(true);
  }

  function clearChat() {
    selectedChat = '';
    selectedMessage = null;
    void loadMessages(true);
  }

  function searchMessages() {
    selectedMessage = null;
    void loadMessages(true);
  }

  function searchChats() {
    void loadChats();
  }

  function prevPage() {
    offset = Math.max(0, offset - PAGE_SIZE);
    void loadMessages(false);
  }

  function nextPage() {
    if (offset + PAGE_SIZE < totalMessages) {
      offset += PAGE_SIZE;
      void loadMessages(false);
    }
  }

  function formatTime(value) {
    if (!value) {
      return '--';
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return value;
    }
    const pad = (num) => String(num).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function trimMiddle(value, max = 52) {
    const text = String(value || '');
    if (text.length <= max) {
      return text;
    }
    const half = Math.floor((max - 3) / 2);
    return `${text.slice(0, half)}...${text.slice(text.length - half)}`;
  }

  function splitQrActivity(lines) {
    let start = -1;
    let end = -1;

    for (let index = 0; index < lines.length; index += 1) {
      if (isQrLine(lines[index])) {
        if (start === -1) {
          start = index;
        }
        end = index;
      } else if (start !== -1 && end !== -1 && index - end > 2) {
        break;
      }
    }

    if (start === -1 || end === -1) {
      return { qrLines: [], logLines: lines };
    }

    return {
      qrLines: lines.slice(start, end + 1),
      logLines: [...lines.slice(0, start), ...lines.slice(end + 1)]
    };
  }

  function isQrLine(line) {
    const text = String(line || '');
    return /[█▄▀]{8,}/.test(text);
  }

  function buildTerminalLogText(logLines) {
    if (logLines.length === 0) {
      return 'Login output will appear here.';
    }
    const maxLines = 180;
    const visibleLines = logLines.slice(Math.max(0, logLines.length - maxLines));
    const hiddenCount = logLines.length - visibleLines.length;
    if (hiddenCount > 0) {
      return [`... ${hiddenCount} older terminal lines hidden ...`, ...visibleLines].join('\n');
    }
    return visibleLines.join('\n');
  }

  function queueTerminalScroll() {
    if (!detailsExpanded || !logPane || typeof requestAnimationFrame === 'undefined') {
      return;
    }
    if (terminalScrollFrame !== null) {
      cancelAnimationFrame(terminalScrollFrame);
    }
    terminalScrollFrame = requestAnimationFrame(() => {
      terminalScrollFrame = null;
      if (logPane) {
        logPane.scrollTop = logPane.scrollHeight;
      }
    });
  }
</script>

<div class="desktop s7-root">
  <section class="app-window">
    <TitleBar title="WhatsApp Backup" />

    <div class="app-content" class:login-mode={!authenticated || authRunning}>
      {#if errorMessage}
        <ErrorBanner message={errorMessage} onclose={() => (errorMessage = '')} />
      {/if}

      <section class="status-shell">
        <ExpandableSection
          label={statusSummary}
          expanded={detailsExpanded}
          onchange={handleDetailsExpanded}
        >
          <div class="status-expanded-content">
            <div class="status-detail-grid">
              <p>
                {#if authenticated}
                  Linked account: {authIdentity || 'authenticated'}
                {:else}
                  Start login and scan the QR code from WhatsApp Linked Devices.
                {/if}
              </p>
              <p class="muted inline-help-row">
                <BalloonHelp message={ARCHIVE_HELP} position="bottom" delay={400}>
                  <span class="help-target">Archive: {status?.archive_dir || '/host-data/archive'} | Text records: {status?.export?.last_indexed_count || 0}</span>
                </BalloonHelp>
              </p>
              <p class="muted inline-help-row">
                <BalloonHelp message={SYNC_SAFETY_HELP} position="bottom" delay={400}>
                  <span class="help-target">Sync safety: reconnect backoff {syncBackoffRemaining > 0 ? `${syncBackoffRemaining}s remaining` : 'idle'} | {notificationLabel}</span>
                </BalloonHelp>
              </p>
              <p class="inline-help-row">
                <BalloonHelp message={SYNC_STATE_HELP} position="bottom" delay={400}>
                  <span class={`sync-state-pill ${syncMeterState}`} aria-label={syncMeterLabel} role="status">
                    <span class="sync-state-dot"></span>
                    <span>{syncMeterLabel}</span>
                  </span>
                </BalloonHelp>
                <BalloonHelp message={EXPORT_HELP} position="bottom" delay={400}>
                  <span class="help-target">{exportInfo.last_export_at ? `Last export ${formatTime(exportInfo.last_export_at)}` : 'No export yet'}</span>
                </BalloonHelp>
              </p>
              {#if exportStatusText}
                <p class="export-status">{exportStatusText}</p>
              {/if}
              {#if exportInfo.last_export_error}
                <p class="error-text">{exportInfo.last_export_error}</p>
              {/if}
              {#if notificationInfo.last_email_error}
                <p class="error-text">Email alert error: {notificationInfo.last_email_error}</p>
              {/if}
            </div>

            {#if activityDisplay.qrLines.length > 0}
              <div class="qr-code-wrap" aria-label="WhatsApp login QR code">
                <pre class="qr-code">{activityDisplay.qrLines.join('\n')}</pre>
              </div>
            {/if}

            {#if displayLines.length > 0}
              <div class="activity-block">
                <div class="activity-block-heading inline-help-row">
                  <BalloonHelp message={WACLI_ACTIVITY_HELP} position="bottom" delay={400}>
                    <strong class="help-target">wacli activity</strong>
                  </BalloonHelp>
                </div>
                <pre class="activity-log" bind:this={logPane}>{terminalLogText}</pre>
              </div>
            {/if}
          </div>
        </ExpandableSection>

        <div class="status-actions">
          {#if !authenticated || authRunning}
            <BalloonHelp message="Starts wacli auth and shows the WhatsApp Linked Devices QR code in this page." position="bottom" delay={500}>
              <Button onclick={startLogin} disabled={authRunning || actionPending === 'login'}>
                {authRunning ? 'Login Running' : 'Start Login'}
              </Button>
            </BalloonHelp>
          {:else}
            {#if !syncRunning}
              <BalloonHelp message="Starts continuous wacli sync. The app will keep exporting synced messages to plain-text files." position="bottom" delay={500}>
                <Button onclick={startSync} disabled={actionPending === 'sync' || syncBackoffRemaining > 0}>
                  {syncBackoffRemaining > 0 ? 'Backoff Active' : 'Start Sync'}
                </Button>
              </BalloonHelp>
            {/if}
            <Button onclick={logout} disabled={Boolean(actionPending)}>Logout</Button>
          {/if}
          <BalloonHelp message={EXPORT_HELP} position="bottom" delay={500}>
            <Button onclick={runExport} disabled={actionPending === 'export'}>
              {actionPending === 'export' ? 'Exporting...' : 'Export Now'}
            </Button>
          </BalloonHelp>
        </div>
      </section>

      {#if authenticated && !authRunning}
      <section class="workspace">
        <aside class="chat-pane">
          <div class="pane-toolbar">
            <input
              class="s7-input"
              placeholder="Filter chats"
              bind:value={chatQuery}
              oninput={searchChats}
            />
            <Button onclick={clearChat} disabled={!selectedChat}>All</Button>
          </div>

          <DataTable
            columns={chatColumns}
            loading={loading}
            loadingText="Loading chats..."
            empty={chats.length === 0 && !loading}
            emptyText="No exported chats yet."
          >
            {#each chats as chat (chat.jid)}
              <tr
                class:selected={chat.jid === selectedChat}
                tabindex="0"
                onclick={() => selectChat(chat.jid)}
                onkeydown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') selectChat(chat.jid);
                }}
              >
                <td title={chat.jid}>
                  <strong>{chat.name || chat.jid}</strong>
                  <span>{trimMiddle(chat.jid, 42)}</span>
                </td>
                <td>{chat.count}</td>
                <td>{formatTime(chat.last_message_at)}</td>
              </tr>
            {/each}
          </DataTable>
        </aside>

        <main class="message-pane">
          <div class="pane-toolbar message-toolbar">
            <div>
              <strong>{selectedChatName}</strong>
              <span>{totalMessages} messages</span>
            </div>
            <input
              class="s7-input"
              placeholder="Search messages"
              bind:value={searchQuery}
              onkeydown={(event) => {
                if (event.key === 'Enter') searchMessages();
              }}
            />
            <Button onclick={searchMessages}>Search</Button>
          </div>

          <div class="message-grid">
            <div class="message-table">
              <DataTable
                columns={messageColumns}
                loading={messagesLoading}
                loadingText="Loading messages..."
                empty={messages.length === 0 && !messagesLoading}
                emptyText="No messages found."
              >
                {#each messages as message (message.file)}
                  <tr
                    class:selected={selectedMessage?.file === message.file}
                    tabindex="0"
                    onclick={() => (selectedMessage = message)}
                    onkeydown={(event) => {
                      if (event.key === 'Enter' || event.key === ' ') selectedMessage = message;
                    }}
                  >
                    <td>{formatTime(message.timestamp)}</td>
                    <td>{message.sender_label}</td>
                    <td title={message.text}>{message.summary || '(empty)'}</td>
                    <td>{message.media_type || '--'}</td>
                  </tr>
                {/each}
              </DataTable>
            </div>

            <aside class="detail-pane">
              {#if selectedMessage}
                <h2>{selectedMessage.sender_label}</h2>
                <p class="muted">{formatTime(selectedMessage.timestamp)} | {selectedMessage.direction}</p>
                <p class="muted">{selectedMessage.chat_name || selectedMessage.chat_jid}</p>
                <pre class="message-body">{selectedMessage.text || '(no text body)'}</pre>
                {#if selectedMessage.media_type}
                  <div class="media-box">
                    <strong>{selectedMessage.media_type}</strong>
                    <span>{selectedMessage.media_filename || selectedMessage.media_mime || 'media attachment'}</span>
                    {#if selectedMessage.media_url}
                      {#if selectedMessage.media_type === 'image'}
                        <img src={selectedMessage.media_url} alt="WhatsApp media" />
                      {/if}
                      <a href={selectedMessage.media_url} target="_blank" rel="noreferrer">Open media</a>
                    {:else}
                      <span class="muted">Media metadata is backed up; file not downloaded yet.</span>
                    {/if}
                  </div>
                {/if}
                <p class="file-path">Text file: {selectedMessage.file}</p>
              {:else}
                <div class="empty-detail">Select a message to inspect its plain-text backup record.</div>
              {/if}
            </aside>
          </div>

          <div class="pagination">
            <Button onclick={prevPage} disabled={offset === 0}>&lt; Prev</Button>
            <span>{pageStart}-{pageEnd} of {totalMessages}</span>
            <Button onclick={nextPage} disabled={offset + PAGE_SIZE >= totalMessages}>Next &gt;</Button>
          </div>
        </main>
      </section>
      {/if}
    </div>
  </section>
</div>
