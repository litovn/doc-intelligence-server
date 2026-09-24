"use client";

import { useCallback, useEffect, useRef, useState, type DragEvent } from "react";
import {
  ApiError,
  deleteDocument,
  listDocuments,
  listTags,
  uploadDocument,
  type AccessLevel,
  type DocumentRecord,
  type Tag,
} from "@/lib/api";
import { useAuth } from "./auth-context";

export default function DocumentsPage() {
  const { level } = useAuth();

  const [docs, setDocs] = useState<DocumentRecord[]>([]);
  const [tags, setTags] = useState<Tag[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  const [visibility, setVisibility] = useState<AccessLevel>("employee");
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);

  const fileInputRef = useRef<HTMLInputElement>(null);

  const refreshDocuments = useCallback(
    () =>
      listDocuments().then(
        (fresh) => {
          setDocs(fresh);
          setLoadError(null);
        },
        (err: unknown) => {
          setLoadError(err instanceof ApiError ? err.message : "Could not load documents.");
        }
      ),
    []
  );

  useEffect(() => {
    refreshDocuments();
    listTags()
      .then(setTags)
      .catch(() => {
        // Tag list failing isn't fatal for viewing the table; the upload
        // form will just show no tag options until a retry (page refresh).
      });
  }, [refreshDocuments]);

  // Poll every 2s only while a row is still "processing" — the interval is
  // owned by this effect, so it stops the moment that stops being true and
  // is always cleared on unmount. A failed poll leaves `docs` (and therefore
  // `processing`) untouched, so a transient error doesn't end the polling.
  // ponytail: setInterval, not a self-rescheduling chain — overlapping GETs
  // would only matter if listing documents started taking longer than 2s.
  const processing = docs.some((d) => d.status === "processing");
  useEffect(() => {
    if (!processing) return;
    const timer = setInterval(refreshDocuments, 2000);
    return () => clearInterval(timer);
  }, [processing, refreshDocuments]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 4000);
    return () => clearTimeout(t);
  }, [toast]);

  function addFiles(files: FileList | File[]) {
    setPendingFiles((prev) => [...prev, ...Array.from(files)]);
  }

  function removePendingFile(index: number) {
    setPendingFiles((prev) => prev.filter((_, i) => i !== index));
  }

  function handleDrop(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragOver(false);
    if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
  }

  // One POST per file, so a failure is attributable to a file: the remaining
  // files are still attempted, only the failed ones stay in the list for a
  // retry, and the table is refreshed either way so the uploads that did
  // succeed show up immediately.
  async function handleUpload() {
    if (!pendingFiles.length || !selectedTags.length) return;
    setUploading(true);
    setUploadError(null);
    const failed: File[] = [];
    let firstError = "";
    for (const file of pendingFiles) {
      try {
        const result = await uploadDocument(
          file,
          selectedTags,
          level === "manager" ? visibility : undefined
        );
        if ("already_present" in result) {
          setToast(`${file.name} is already in the knowledge base.`);
        }
      } catch (err) {
        failed.push(file);
        firstError ||= `${file.name}: ${err instanceof ApiError ? err.message : "upload failed"}`;
      }
    }
    setPendingFiles(failed);
    setUploadError(
      failed.length > 1 ? `${firstError} (and ${failed.length - 1} more)` : firstError || null
    );
    setUploading(false);
    await refreshDocuments();
  }

  async function handleDelete(doc: DocumentRecord) {
    if (!window.confirm(`Delete "${doc.name}"? This cannot be undone.`)) return;
    try {
      await deleteDocument(doc.document_id);
      await refreshDocuments();
    } catch (err) {
      setLoadError(err instanceof ApiError ? err.message : "Delete failed.");
    }
  }

  return (
    <div>
      <h1>Documents</h1>
      {toast && <div className="toast">{toast}</div>}

      <section className="upload-panel">
        <div
          className={`dropzone${dragOver ? " dropzone-active" : ""}`}
          onClick={() => fileInputRef.current?.click()}
          onDragOver={(e) => {
            e.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={handleDrop}
        >
          <p>Drag and drop files here, or click to choose files.</p>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            hidden
            onChange={(e) => {
              if (e.target.files) addFiles(e.target.files);
              e.target.value = "";
            }}
          />
        </div>

        {pendingFiles.length > 0 && (
          <ul className="pending-files">
            {pendingFiles.map((f, i) => (
              <li key={`${f.name}-${i}`}>
                {f.name}
                <button
                  type="button"
                  onClick={() => removePendingFile(i)}
                  aria-label={`Remove ${f.name}`}
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
        )}

        <div className="upload-controls">
          <label>
            Tags
            <select
              multiple
              value={selectedTags}
              onChange={(e) =>
                setSelectedTags(Array.from(e.target.selectedOptions, (o) => o.value))
              }
            >
              {tags.map((t) => (
                <option key={t.tag} value={t.tag}>
                  {t.tag}
                </option>
              ))}
            </select>
          </label>

          {level === "manager" && (
            <label>
              Visibility
              <select
                value={visibility}
                onChange={(e) => setVisibility(e.target.value as AccessLevel)}
              >
                <option value="employee">Everyone</option>
                <option value="manager">Managers only</option>
              </select>
            </label>
          )}

          <button
            type="button"
            onClick={handleUpload}
            disabled={uploading || !pendingFiles.length || !selectedTags.length}
          >
            {uploading ? "Uploading…" : "Upload"}
          </button>
        </div>
        {pendingFiles.length > 0 && !selectedTags.length && (
          <p className="hint">Select at least one tag before uploading.</p>
        )}
        {uploadError && <p className="form-error">{uploadError}</p>}
      </section>

      {loadError && <p className="form-error">{loadError}</p>}

      <table className="documents-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Tags</th>
            <th>Status</th>
            <th>Pages</th>
            <th>Chunks</th>
            <th>Uploaded</th>
            <th>Error</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {docs.map((doc) => (
            <tr key={doc.document_id}>
              <td>{doc.name}</td>
              <td>
                {doc.tags.map((t) => (
                  <span key={t} className="chip">
                    {t}
                  </span>
                ))}
                {doc.required_level === "manager" && (
                  <span className="chip chip-visibility">Managers only</span>
                )}
              </td>
              <td>
                <span className={`badge badge-${doc.status}`}>{doc.status}</span>
              </td>
              <td>{doc.page_count ?? "–"}</td>
              <td>{doc.chunk_count ?? "–"}</td>
              <td>{new Date(doc.uploaded_at).toLocaleString()}</td>
              <td className="error-cell">{doc.error ?? ""}</td>
              <td>
                <button type="button" onClick={() => handleDelete(doc)}>
                  Delete
                </button>
              </td>
            </tr>
          ))}
          {docs.length === 0 && (
            <tr>
              <td colSpan={8}>No documents yet.</td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
