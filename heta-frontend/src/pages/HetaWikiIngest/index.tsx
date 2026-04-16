import { useState, useRef } from 'react';
import { Upload, FilePlus, GitMerge } from 'lucide-react';
import PageShell from '../../components/layout/PageShell';
import Button from '../../components/ui/Button';
import { API } from '../../api/endpoints';
import styles from './HetaWikiIngest.module.css';

// ── Types ─────────────────────────────────────────────────────────────────────

interface TaskPoll { status: string; message?: string; error?: string; result?: Record<string, unknown> }

type Mode = 'new' | 'merge';

const MODES: { id: Mode; Icon: typeof FilePlus; label: string; desc: string }[] = [
  {
    id: 'new',
    Icon: FilePlus,
    label: 'Add as new page',
    desc: 'Fast. Creates a standalone wiki page from this document.',
  },
  {
    id: 'merge',
    Icon: GitMerge,
    label: 'Integrate with wiki',
    desc: 'Slower. Agent reads existing pages and merges intelligently.',
  },
];

// ── Helpers ───────────────────────────────────────────────────────────────────

const BASE = 'http://localhost:8000';

const SUPPORTED = ['.pdf', '.docx', '.doc', '.ppt', '.pptx', '.md', '.txt'];

function pollTask(
  taskId: string,
  onProgress: (msg: string) => void,
  onDone: (r: TaskPoll) => void,
  onFail: (e: string) => void,
) {
  const iv = setInterval(async () => {
    try {
      const res  = await fetch(`${BASE}${API.wiki.task(taskId)}`);
      const data = (await res.json()) as TaskPoll;
      if (data.message) onProgress(data.message);
      if (data.status === 'completed')                 { clearInterval(iv); onDone(data); }
      if (data.status === 'failed' || data.status === 'cancelled') {
        clearInterval(iv); onFail(data.error ?? 'unknown error');
      }
    } catch { clearInterval(iv); onFail('network error'); }
  }, 1500);
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function HetaWikiIngestPage() {
  const [file, setFile]       = useState<File | null>(null);
  const [mode, setMode]       = useState<Mode>('new');
  const [loading, setLoading] = useState(false);
  const [msg, setMsg]         = useState('');
  const [status, setStatus]   = useState<'idle' | 'ok' | 'error'>('idle');
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  function pickFile(f: File) {
    const ext = '.' + f.name.split('.').pop()?.toLowerCase();
    if (!SUPPORTED.includes(ext)) {
      setMsg(`Unsupported file type: ${ext}`);
      setStatus('error');
      return;
    }
    setFile(f);
    setMsg('');
    setStatus('idle');
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f) pickFile(f);
  }

  async function submit() {
    if (!file || loading) return;
    setLoading(true); setStatus('idle'); setMsg('uploading…');
    try {
      const fd = new FormData();
      fd.append('file', file);
      fd.append('merge', String(mode === 'merge'));
      const res  = await fetch(`${BASE}${API.wiki.ingest}`, { method: 'POST', body: fd });
      const data = await res.json();
      if (!res.ok) { setStatus('error'); setMsg(data.detail ?? 'upload failed'); setLoading(false); return; }
      setMsg('processing…');
      pollTask(
        data.task_id,
        (m) => setMsg(m),
        (r) => {
          const title = r.result?.title as string | undefined;
          setMsg(title ? `Done — "${title}"` : 'Done');
          setStatus('ok'); setLoading(false);
        },
        (e) => { setMsg(e); setStatus('error'); setLoading(false); },
      );
    } catch (e) { setMsg(String(e)); setStatus('error'); setLoading(false); }
  }

  return (
    <div className={styles.layout}>
      <div className={styles.main}>
        <PageShell title="Ingest">
          <div className={styles.center}>

            {/* ── Drop zone ── */}
            <div
              className={[styles.dropzone, dragging ? styles.dropzoneOver : '', file ? styles.dropzoneHasFile : ''].filter(Boolean).join(' ')}
              onDrop={onDrop}
              onDragOver={e => { e.preventDefault(); setDragging(true); }}
              onDragLeave={() => setDragging(false)}
              onClick={() => inputRef.current?.click()}
            >
              <Upload size={22} className={styles.dropIcon} />
              {file ? (
                <>
                  <span className={styles.dropFilename}>{file.name}</span>
                  <span className={styles.dropMeta}>{(file.size / 1024).toFixed(1)} KB</span>
                </>
              ) : (
                <>
                  <span className={styles.dropText}>Drag & drop or click to select</span>
                  <span className={styles.dropMeta}>{SUPPORTED.join(' · ')}</span>
                </>
              )}
              <input ref={inputRef} type="file" hidden onChange={e => { const f = e.target.files?.[0]; if (f) pickFile(f); }} />
            </div>

            {/* ── Mode cards ── */}
            <div className={styles.modeGrid}>
              {MODES.map(({ id, Icon, label, desc }) => (
                <button
                  key={id}
                  className={[styles.modeCard, mode === id ? styles.modeCardActive : ''].join(' ')}
                  onClick={() => setMode(id)}
                  disabled={loading}
                >
                  <Icon size={18} className={styles.modeIcon} />
                  <span className={styles.modeLabel}>{label}</span>
                  <span className={styles.modeDesc}>{desc}</span>
                </button>
              ))}
            </div>

            {/* ── Action ── */}
            <div className={styles.submitBtn}>
              <Button
                variant="primary"
                loading={loading}
                disabled={!file}
                onClick={submit}
              >
                Ingest
              </Button>
            </div>

            {/* ── Status ── */}
            {msg && (
              <p className={[
                styles.statusMsg,
                status === 'error' ? styles.statusError :
                status === 'ok'    ? styles.statusOk    : styles.statusInfo,
              ].join(' ')}>
                {loading && <span className={styles.spinner} />}
                {msg}
              </p>
            )}

          </div>
        </PageShell>
      </div>
    </div>
  );
}
