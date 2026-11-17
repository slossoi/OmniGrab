const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('api', {
  fetchInfo: (url) => ipcRenderer.send('fetch-info', url),
  onInfoResult: (callback) => {
    const handler = (_event, data) => callback(data);
    ipcRenderer.on('info-result', handler);
    return () => ipcRenderer.removeListener('info-result', handler);
  },
  startDownload: (params) => ipcRenderer.send('start-download', params),
  stopDownload: () => ipcRenderer.send('stop-download'),
  onProgress: (callback) => {
    const handler = (_event, percent) => callback(percent);
    ipcRenderer.on('download-progress', handler);
    return () => ipcRenderer.removeListener('download-progress', handler);
  },
  onLog: (callback) => {
    const handler = (_event, line) => callback(line);
    ipcRenderer.on('download-log', handler);
    return () => ipcRenderer.removeListener('download-log', handler);
  },
  onStatus: (callback) => {
    const handler = (_event, status) => callback(status);
    ipcRenderer.on('download-status', handler);
    return () => ipcRenderer.removeListener('download-status', handler);
  },
  openDownloadsFolder: () => ipcRenderer.send('open-downloads-folder')
});
