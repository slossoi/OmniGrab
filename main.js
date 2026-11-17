const { app, BrowserWindow, ipcMain, shell } = require('electron');
const path = require('path');
const { spawn } = require('child_process');
const os = require('os');

let mainWindow;
let currentDownloadProcess = null;
let currentInfoProcess = null;
let wasDownloadStopped = false;

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 980,
    height: 820,
    minWidth: 800,
    minHeight: 650,
    backgroundColor: '#0c0d12',
    autoHideMenuBar: true,
    title: 'OmniGrab',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false,
      contextIsolation: true
    }
  });

  mainWindow.loadFile(path.join(__dirname, 'index.html'));
}

app.whenReady().then(() => {
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

// Helper to kill process tree on Windows
function killProcessTree(proc) {
  if (!proc || !proc.pid) return;
  try {
    if (process.platform === 'win32') {
      spawn('taskkill', ['/pid', proc.pid.toString(), '/T', '/F']);
    } else {
      proc.kill('SIGKILL');
    }
  } catch (e) {
    console.error('Error killing process:', e);
  }
}

function getAppPaths() {
  const isPackaged = app.isPackaged;
  const baseDir = isPackaged ? process.resourcesPath : __dirname;
  
  let scriptPath = path.join(baseDir, 'downloader.py');
  if (!require('fs').existsSync(scriptPath)) {
    scriptPath = path.join(__dirname, 'downloader.py');
  }

  let binDir = path.join(baseDir, 'bin');
  if (!require('fs').existsSync(binDir)) {
    binDir = path.join(__dirname, 'bin');
  }

  return { isPackaged, baseDir, scriptPath, binDir };
}

// IPC: Fetch Info
ipcMain.on('fetch-info', (event, url) => {
  if (currentInfoProcess) {
    killProcessTree(currentInfoProcess);
    currentInfoProcess = null;
  }

  const { scriptPath, binDir, baseDir } = getAppPaths();
  const envPath = binDir + path.delimiter + (process.env.PATH || '');

  currentInfoProcess = spawn('python', ['-u', scriptPath, 'info', url.trim()], {
    cwd: baseDir,
    env: { ...process.env, PATH: envPath, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' }
  });

  let outputBuffer = '';

  currentInfoProcess.stdout.on('data', (data) => {
    outputBuffer += data.toString('utf-8');
  });

  currentInfoProcess.stderr.on('data', (data) => {
    console.error('info stderr:', data.toString('utf-8'));
  });

  currentInfoProcess.on('close', (code) => {
    currentInfoProcess = null;
    const lines = outputBuffer.split(/\r?\n/);
    let parsed = null;
    for (const line of lines) {
      if (line.startsWith('__INFO_JSON__:')) {
        try {
          parsed = JSON.parse(line.substring('__INFO_JSON__:'.length));
          break;
        } catch (e) {
          console.error('Failed to parse info json:', e);
        }
      }
    }
    if (parsed) {
      event.sender.send('info-result', parsed);
    } else {
      event.sender.send('info-result', { success: false, error: 'Не удалось получить метаданные' });
    }
  });

  currentInfoProcess.on('error', (err) => {
    currentInfoProcess = null;
    event.sender.send('info-result', { success: false, error: err.message });
  });
});

// IPC: Start Download
ipcMain.on('start-download', (event, { url, quality, bitrate, tiktokMode, soundcloudMode }) => {
  if (!url || !url.trim()) {
    event.sender.send('download-log', '[Ошибка] Ссылка не может быть пустой.');
    event.sender.send('download-status', { inProgress: false, code: 1 });
    return;
  }

  if (currentDownloadProcess) {
    event.sender.send('download-log', '[Внимание] Загрузка уже выполняется.');
    return;
  }

  wasDownloadStopped = false;
  event.sender.send('download-status', { inProgress: true });
  event.sender.send('download-log', `[Запуск] Начинаем скачивание: ${url.trim()}`);

  const { scriptPath, binDir, baseDir } = getAppPaths();
  const envPath = binDir + path.delimiter + (process.env.PATH || '');

  const args = ['-u', scriptPath, 'download', url.trim()];

  if (quality) {
    args.push('--quality', quality);
  }
  if (bitrate) {
    args.push('--bitrate', bitrate);
  }
  if (tiktokMode) {
    args.push('--tiktok-mode', tiktokMode);
  }
  if (soundcloudMode) {
    args.push('--soundcloud-mode', soundcloudMode);
  }

  currentDownloadProcess = spawn('python', args, {
    cwd: baseDir,
    env: { ...process.env, PATH: envPath, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' }
  });

  currentDownloadProcess.stdout.on('data', (data) => {
    const text = data.toString('utf-8');
    const lines = text.split(/\r?\n/);
    lines.forEach(line => {
      const trimmed = line.trim();
      if (!trimmed) return;
      if (trimmed.startsWith('__PROGRESS__:')) {
        const pct = parseFloat(trimmed.substring('__PROGRESS__:'.length));
        if (!isNaN(pct)) {
          event.sender.send('download-progress', pct);
        }
      } else {
        event.sender.send('download-log', line);
      }
    });
  });

  currentDownloadProcess.stderr.on('data', (data) => {
    const lines = data.toString('utf-8').split(/\r?\n/);
    lines.forEach(line => {
      if (line.trim()) {
        event.sender.send('download-log', `[система] ${line}`);
      }
    });
  });

  currentDownloadProcess.on('close', (code) => {
    const procRef = currentDownloadProcess;
    currentDownloadProcess = null;
    const stopped = wasDownloadStopped;

    event.sender.send('download-status', { inProgress: false, code, stopped });

    if (stopped) {
      event.sender.send('download-log', '⏹ Загрузка остановлена пользователем.');
    } else if (code === 0) {
      event.sender.send('download-log', '✔ Загрузка успешно завершена!');
    } else {
      event.sender.send('download-log', `✖ Загрузка прервана с кодом: ${code}`);
    }
  });

  currentDownloadProcess.on('error', (err) => {
    currentDownloadProcess = null;
    event.sender.send('download-status', { inProgress: false, code: 1, stopped: false });
    event.sender.send('download-log', `[Системная ошибка] Не удалось запустить downloader: ${err.message}`);
  });
});

// IPC: Stop Download
ipcMain.on('stop-download', (event) => {
  if (currentDownloadProcess) {
    wasDownloadStopped = true;
    killProcessTree(currentDownloadProcess);
    currentDownloadProcess = null;
    event.sender.send('download-status', { inProgress: false, stopped: true });
    event.sender.send('download-log', '⏹ Остановка процесса...');
  }
});

// IPC: Open Downloads Folder
ipcMain.on('open-downloads-folder', () => {
  const downloadsDir = path.join(os.homedir(), 'Downloads');
  shell.openPath(downloadsDir);
});
