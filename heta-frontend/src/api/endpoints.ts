// All backend API endpoint definitions — edit here when routes change

const BASE = '/api/v1/hetadb/files';

export const API = {
  dataset: {
    list:           `${BASE}/raw-files/datasets`,
    create:         `${BASE}/raw-files/datasets`,
    delete:         (name: string) => `${BASE}/raw-files/datasets/${name}`,
    listFiles:      (name: string) => `${BASE}/raw-files/datasets/${name}/files`,
    uploadFiles:    (name: string) => `${BASE}/raw-files/datasets/${name}/files`,
    deleteFile:     (name: string, filename: string) =>
      `${BASE}/raw-files/datasets/${name}/files/${filename}`,
    uploadInit:     (name: string) => `${BASE}/raw-files/datasets/${name}/upload/init`,
    uploadChunk:    (name: string, id: string) => `${BASE}/raw-files/datasets/${name}/upload/${id}/chunk`,
    uploadComplete: (name: string, id: string) => `${BASE}/raw-files/datasets/${name}/upload/${id}/complete`,
    uploadAbort:    (name: string, id: string) => `${BASE}/raw-files/datasets/${name}/upload/${id}`,
  },
  kb: {
    list:   `${BASE}/knowledge-bases`,
    create: `${BASE}/knowledge-bases`,
    detail: (name: string) => `${BASE}/knowledge-bases/${name}`,
    delete: (name: string) => `${BASE}/knowledge-bases/${name}`,
    parse:  (name: string) => `${BASE}/knowledge-bases/${name}/parse`,
  },
  task: {
    list:   `${BASE}/processing/tasks`,
    detail: (id: string) => `${BASE}/processing/tasks/${id}`,
    cancel: (id: string) => `${BASE}/processing/tasks/${id}/cancel`,
  },
  chat: '/api/v1/hetadb/chat',

  wiki: {
    ingest: '/api/v1/hetawiki/ingest',
    query:  '/api/v1/hetawiki/query',
    lint:   '/api/v1/hetawiki/lint',
    graph:  '/api/v1/hetawiki/graph',
    page:   (filename: string) => `/api/v1/hetawiki/pages/${encodeURIComponent(filename)}`,
    task:   (id: string) => `/api/v1/hetawiki/tasks/${id}`,
  },
} as const;
