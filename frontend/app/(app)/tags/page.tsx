"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import {
  ApiError,
  createTag,
  deleteTag,
  listTags,
  updateTagDescription,
  type Tag,
} from "@/lib/api";

export default function TagsPage() {
  const [tags, setTags] = useState<Tag[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const [newName, setNewName] = useState("");
  const [newDescription, setNewDescription] = useState("");
  const [creating, setCreating] = useState(false);
  // Name of the one tag whose description is open for editing, if any.
  const [editing, setEditing] = useState<string | null>(null);

  const refresh = useCallback(
    () =>
      listTags().then(
        (fresh) => {
          setTags(fresh);
          setLoadError(null);
        },
        (err: unknown) => {
          setLoadError(err instanceof ApiError ? err.message : "Could not load tags.");
        }
      ),
    []
  );

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function handleCreate(e: FormEvent) {
    e.preventDefault();
    setCreating(true);
    setActionError(null);
    try {
      await createTag(newName, newDescription);
      setNewName("");
      setNewDescription("");
      await refresh();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Could not create tag.");
    } finally {
      setCreating(false);
    }
  }

  async function handleSaveDescription(name: string, e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const description = String(new FormData(e.currentTarget).get("description") ?? "");
    setActionError(null);
    try {
      await updateTagDescription(name, description);
      setEditing(null);
      await refresh();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : `Could not update "${name}".`);
    }
  }

  async function handleDelete(tag: Tag) {
    if (!window.confirm(`Delete tag "${tag.tag}"?`)) return;
    setActionError(null);
    try {
      await deleteTag(tag.tag);
      await refresh();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setActionError(`Cannot delete "${tag.tag}": ${describeTagConflict(err)}`);
      } else {
        setActionError(err instanceof ApiError ? err.message : `Could not delete "${tag.tag}".`);
      }
    }
  }

  return (
    <div>
      <h1>Tag Vocabulary</h1>

      <form className="tag-create-form" onSubmit={handleCreate}>
        <input
          placeholder="name"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          required
        />
        <input
          placeholder="description"
          value={newDescription}
          onChange={(e) => setNewDescription(e.target.value)}
          required
        />
        <button type="submit" disabled={creating}>
          {creating ? "Creating…" : "Create tag"}
        </button>
      </form>

      {actionError && <p className="form-error">{actionError}</p>}
      {loadError && <p className="form-error">{loadError}</p>}

      <table className="tags-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Description</th>
            <th>Documents</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {tags.map((tag) => (
            <tr key={tag.tag}>
              <td>{tag.tag}</td>
              <td className="description-cell">
                {editing === tag.tag ? (
                  <form
                    className="description-form"
                    onSubmit={(e) => handleSaveDescription(tag.tag, e)}
                  >
                    <input
                      name="description"
                      defaultValue={tag.description}
                      aria-label={`Description of ${tag.tag}`}
                      required
                      autoFocus
                    />
                    <button type="submit">Save</button>
                    <button type="button" onClick={() => setEditing(null)}>
                      Cancel
                    </button>
                  </form>
                ) : (
                  tag.description
                )}
              </td>
              <td>{tag.document_count}</td>
              <td>
                <div className="row-actions">
                  <button
                    type="button"
                    onClick={() => setEditing(tag.tag)}
                    disabled={editing === tag.tag}
                  >
                    Modify
                  </button>
                  <button type="button" onClick={() => handleDelete(tag)}>
                    Delete
                  </button>
                </div>
              </td>
            </tr>
          ))}
          {tags.length === 0 && (
            <tr>
              <td colSpan={4}>No tags yet.</td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

// The 409 body shape isn't nailed down by the API contract beyond "includes
// the blocking document list" — parsed defensively so a shape mismatch
// degrades to a generic message instead of throwing.
function describeTagConflict(err: ApiError): string {
  const detail = (err.body as { detail?: unknown } | undefined)?.detail;
  if (detail && typeof detail === "object") {
    const documents = (detail as { documents?: unknown }).documents;
    if (Array.isArray(documents) && documents.length > 0) {
      const names = documents.map((doc) => {
        if (typeof doc === "string") return doc;
        const d = doc as { filename?: string; name?: string; id?: string } | null;
        return d?.filename ?? d?.name ?? d?.id ?? "unknown document";
      });
      return `still used by ${names.join(", ")}`;
    }
    const message = (detail as { message?: unknown }).message;
    if (typeof message === "string") return message;
  }
  if (typeof detail === "string") return detail;
  return "still in use by one or more documents.";
}
