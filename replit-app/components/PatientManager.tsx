"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

interface Patient {
  id: string;
  mrn: string;
  first_name: string | null;
  last_name: string | null;
  birth_date: string | null;
  gender: string | null;
}

interface PatientManagerProps {
  patients: Patient[];
}

type Mode = "list" | "add" | "edit" | "confirm-delete";

const EMPTY_FORM = {
  mrn: "", first_name: "", last_name: "",
  birth_date: "", gender: "", race: "", ethnicity: "",
  address_line: "", city: "", state: "", zip_code: "", insurance_type: "",
};

export default function PatientManager({ patients: initialPatients }: PatientManagerProps) {
  const router = useRouter();
  const [mode, setMode] = useState<Mode>("list");
  const [patients, setPatients] = useState<Patient[]>(initialPatients);
  const [selected, setSelected] = useState<Patient | null>(null);
  const [form, setForm] = useState(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [search, setSearch] = useState("");

  const filtered = patients.filter((p) => {
    const q = search.toLowerCase();
    return (
      (p.first_name || "").toLowerCase().includes(q) ||
      (p.last_name || "").toLowerCase().includes(q) ||
      p.mrn.toLowerCase().includes(q)
    );
  });

  function openAdd() {
    setForm(EMPTY_FORM);
    setError(null);
    setSuccess(null);
    setMode("add");
  }

  function openEdit(p: Patient) {
    setSelected(p);
    setForm({
      mrn: p.mrn,
      first_name: p.first_name || "",
      last_name: p.last_name || "",
      birth_date: p.birth_date ? p.birth_date.slice(0, 10) : "",
      gender: p.gender || "",
      race: "", ethnicity: "", address_line: "", city: "",
      state: "", zip_code: "", insurance_type: "",
    });
    setError(null);
    setSuccess(null);
    setMode("edit");
  }

  function openDelete(p: Patient) {
    setSelected(p);
    setError(null);
    setMode("confirm-delete");
  }

  function cancel() {
    setMode("list");
    setSelected(null);
    setError(null);
    setSuccess(null);
  }

  async function handleAdd() {
    if (!form.mrn.trim()) { setError("MRN is required"); return; }
    setSaving(true); setError(null);
    try {
      const res = await fetch("/api/patients", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...form,
          first_name: form.first_name || null,
          last_name: form.last_name || null,
          birth_date: form.birth_date || null,
          gender: form.gender || null,
          race: form.race || null,
          ethnicity: form.ethnicity || null,
          address_line: form.address_line || null,
          city: form.city || null,
          state: form.state || null,
          zip_code: form.zip_code || null,
          insurance_type: form.insurance_type || null,
        }),
      });
      const data = await res.json();
      if (!res.ok) { setError(data.error || "Failed to add patient"); return; }
      setPatients((prev) => [...prev, data.patient]);
      setSuccess(`Patient ${data.patient.mrn} added successfully.`);
      setMode("list");
      router.refresh();
    } catch {
      setError("Network error");
    } finally {
      setSaving(false);
    }
  }

  async function handleEdit() {
    if (!selected) return;
    setSaving(true); setError(null);
    try {
      const payload: Record<string, string | null> = {};
      if (form.first_name !== undefined) payload.first_name = form.first_name || null;
      if (form.last_name !== undefined) payload.last_name = form.last_name || null;
      if (form.birth_date) payload.birth_date = form.birth_date;
      if (form.gender) payload.gender = form.gender;
      if (form.race) payload.race = form.race;
      if (form.ethnicity) payload.ethnicity = form.ethnicity;
      if (form.address_line) payload.address_line = form.address_line;
      if (form.city) payload.city = form.city;
      if (form.state) payload.state = form.state;
      if (form.zip_code) payload.zip_code = form.zip_code;
      if (form.insurance_type) payload.insurance_type = form.insurance_type;

      const res = await fetch(`/api/patients/${selected.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) { setError(data.error || "Failed to update patient"); return; }
      setPatients((prev) =>
        prev.map((p) => (p.id === selected.id ? { ...p, ...data.patient } : p))
      );
      setSuccess(`Patient ${data.patient.mrn} updated successfully.`);
      setMode("list");
      router.refresh();
    } catch {
      setError("Network error");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    if (!selected) return;
    setSaving(true); setError(null);
    try {
      const res = await fetch(`/api/patients/${selected.id}`, { method: "DELETE" });
      const data = await res.json();
      if (!res.ok) { setError(data.error || "Failed to delete patient"); return; }
      setPatients((prev) => prev.filter((p) => p.id !== selected.id));
      setSuccess(`Patient ${data.deleted.mrn} removed.`);
      setMode("list");
      router.refresh();
    } catch {
      setError("Network error");
    } finally {
      setSaving(false);
    }
  }

  function field(label: string, key: keyof typeof form, type = "text", required = false) {
    return (
      <div key={key}>
        <label className="block text-xs font-medium text-gray-600 mb-1">
          {label}{required && <span className="text-red-500 ml-1">*</span>}
        </label>
        <input
          type={type}
          value={form[key]}
          onChange={(e) => setForm((f) => ({ ...f, [key]: e.target.value }))}
          className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-gray-800"
          disabled={saving || (mode === "edit" && key === "mrn")}
          placeholder={key === "mrn" ? "e.g. MRN-00123" : ""}
        />
      </div>
    );
  }

  return (
    <div className="rounded-xl border p-4">
      <div className="flex justify-between items-center mb-4">
        <h2 className="text-lg font-semibold">Patient Management</h2>
        {mode === "list" && (
          <button
            onClick={openAdd}
            className="px-3 py-1.5 bg-gray-800 text-white text-sm rounded-lg hover:bg-gray-700"
          >
            + Add Patient
          </button>
        )}
      </div>

      {success && (
        <div className="mb-3 rounded-lg bg-emerald-50 border border-emerald-200 px-3 py-2 text-sm text-emerald-800">
          {success}
        </div>
      )}

      {error && (
        <div className="mb-3 rounded-lg bg-red-50 border border-red-200 px-3 py-2 text-sm text-red-800">
          {error}
        </div>
      )}

      {mode === "list" && (
        <>
          <div className="mb-3">
            <input
              type="text"
              placeholder="Search by name or MRN…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-gray-300"
            />
          </div>
          <div className="overflow-auto max-h-64">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-gray-400 border-b">
                  <th className="pb-2 pr-3">Name</th>
                  <th className="pb-2 pr-3">MRN</th>
                  <th className="pb-2 pr-3">DOB</th>
                  <th className="pb-2 pr-3">Gender</th>
                  <th className="pb-2">Actions</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((p) => (
                  <tr key={p.id} className="border-b last:border-0 hover:bg-gray-50">
                    <td className="py-2 pr-3 font-medium">
                      {p.last_name || "—"}{p.first_name ? `, ${p.first_name}` : ""}
                    </td>
                    <td className="py-2 pr-3 text-gray-500">{p.mrn}</td>
                    <td className="py-2 pr-3 text-gray-500">
                      {p.birth_date ? new Date(p.birth_date).toLocaleDateString() : "—"}
                    </td>
                    <td className="py-2 pr-3 text-gray-500 capitalize">{p.gender || "—"}</td>
                    <td className="py-2">
                      <div className="flex gap-2">
                        <button
                          onClick={() => openEdit(p)}
                          className="text-xs px-2 py-1 rounded border hover:bg-gray-100"
                        >
                          Edit
                        </button>
                        <button
                          onClick={() => openDelete(p)}
                          className="text-xs px-2 py-1 rounded border border-red-200 text-red-600 hover:bg-red-50"
                        >
                          Remove
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
                {filtered.length === 0 && (
                  <tr>
                    <td colSpan={5} className="py-4 text-center text-gray-400 text-sm">
                      No patients match your search.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}

      {(mode === "add" || mode === "edit") && (
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            {field("MRN", "mrn", "text", true)}
            {field("Gender", "gender")}
            {field("First Name", "first_name")}
            {field("Last Name", "last_name")}
            {field("Date of Birth", "birth_date", "date")}
            {field("Insurance Type", "insurance_type")}
            {field("Race", "race")}
            {field("Ethnicity", "ethnicity")}
            {field("Address", "address_line")}
            {field("City", "city")}
            {field("State", "state")}
            {field("ZIP Code", "zip_code")}
          </div>
          <div className="flex gap-2 pt-1">
            <button
              onClick={mode === "add" ? handleAdd : handleEdit}
              disabled={saving}
              className="px-4 py-2 bg-gray-800 text-white text-sm rounded-lg hover:bg-gray-700 disabled:opacity-50"
            >
              {saving ? "Saving…" : mode === "add" ? "Add Patient" : "Save Changes"}
            </button>
            <button
              onClick={cancel}
              disabled={saving}
              className="px-4 py-2 border text-sm rounded-lg hover:bg-gray-50"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {mode === "confirm-delete" && selected && (
        <div className="rounded-lg border border-red-200 bg-red-50 p-4">
          <p className="text-sm font-medium text-red-800 mb-1">
            Remove patient{" "}
            <strong>
              {selected.first_name} {selected.last_name} ({selected.mrn})
            </strong>
            ?
          </p>
          <p className="text-xs text-red-600 mb-3">
            This will permanently delete the patient and all associated records (cascade).
          </p>
          <div className="flex gap-2">
            <button
              onClick={handleDelete}
              disabled={saving}
              className="px-3 py-1.5 bg-red-600 text-white text-sm rounded-lg hover:bg-red-700 disabled:opacity-50"
            >
              {saving ? "Removing…" : "Yes, Remove"}
            </button>
            <button
              onClick={cancel}
              disabled={saving}
              className="px-3 py-1.5 border text-sm rounded-lg hover:bg-gray-50"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
