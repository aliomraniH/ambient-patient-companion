"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { createPatient, updatePatient, deletePatient } from "@/lib/actions";

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

type Mode = "list" | "add" | "edit";

const EMPTY_FORM: Record<string, string> = {
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

  function startAdd() {
    setMode("add");
    setForm(EMPTY_FORM);
    setError(null);
    setSuccess(null);
  }

  function startEdit(patient: Patient) {
    setMode("edit");
    setSelected(patient);
    setForm({
      mrn: patient.mrn,
      first_name: patient.first_name || "",
      last_name: patient.last_name || "",
      birth_date: patient.birth_date ? String(patient.birth_date).split("T")[0] : "",
      gender: patient.gender || "",
      race: "", ethnicity: "", address_line: "", city: "",
      state: "", zip_code: "", insurance_type: "",
    });
    setError(null);
    setSuccess(null);
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
      const result = await createPatient({
        mrn: form.mrn,
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
      });
      setPatients((prev) => [...prev, result.patient]);
      setSuccess(`Patient ${result.patient.mrn} added successfully.`);
      setMode("list");
      router.refresh();
    } catch {
      setError("Failed to add patient");
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

      const result = await updatePatient(selected.id, payload);
      setPatients((prev) =>
        prev.map((p) => (p.id === selected.id ? { ...p, ...result.patient } : p))
      );
      setSuccess(`Patient ${result.patient.mrn} updated successfully.`);
      setMode("list");
      router.refresh();
    } catch {
      setError("Failed to update patient");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    if (!selected) return;
    setSaving(true); setError(null);
    try {
      const result = await deletePatient(selected.id);
      setPatients((prev) => prev.filter((p) => p.id !== selected.id));
      setSuccess(`Patient ${result.deleted.mrn} removed.`);
      setMode("list");
      router.refresh();
    } catch {
      setError("Failed to delete patient");
    } finally {
      setSaving(false);
    }
  }

  const filtered = patients.filter((p) => {
    const q = search.toLowerCase();
    return (
      !q ||
      p.mrn.toLowerCase().includes(q) ||
      (p.first_name || "").toLowerCase().includes(q) ||
      (p.last_name || "").toLowerCase().includes(q)
    );
  });

  if (mode === "add" || mode === "edit") {
    return (
      <div className="rounded-xl border p-6">
        <h2 className="text-lg font-semibold mb-4">
          {mode === "add" ? "Add Patient" : "Edit Patient"}
        </h2>
        {error && <p className="text-red-600 text-sm mb-3">{error}</p>}
        <div className="grid gap-3 sm:grid-cols-2">
          {Object.keys(EMPTY_FORM).map((key) => (
            <label key={key} className="block">
              <span className="text-xs text-gray-500 capitalize">
                {key.replace(/_/g, " ")}
              </span>
              <input
                type={key === "birth_date" ? "date" : "text"}
                className="mt-1 block w-full rounded-lg border px-3 py-2 text-sm"
                value={form[key]}
                onChange={(e) => setForm({ ...form, [key]: e.target.value })}
                disabled={mode === "edit" && key === "mrn"}
              />
            </label>
          ))}
        </div>
        <div className="mt-4 flex gap-2">
          <button
            onClick={mode === "add" ? handleAdd : handleEdit}
            disabled={saving}
            className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700 disabled:opacity-50"
          >
            {saving ? "Saving..." : mode === "add" ? "Create" : "Update"}
          </button>
          {mode === "edit" && (
            <button
              onClick={handleDelete}
              disabled={saving}
              className="px-4 py-2 bg-red-600 text-white rounded-lg text-sm hover:bg-red-700 disabled:opacity-50"
            >
              Delete
            </button>
          )}
          <button
            onClick={cancel}
            className="px-4 py-2 border rounded-lg text-sm hover:bg-gray-50"
          >
            Cancel
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-xl border p-6">
      <div className="flex justify-between items-center mb-4">
        <h2 className="text-lg font-semibold">Patient Management</h2>
        <button
          onClick={startAdd}
          className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700"
        >
          + Add Patient
        </button>
      </div>

      {success && (
        <p className="text-emerald-600 text-sm mb-3">{success}</p>
      )}

      <input
        type="text"
        placeholder="Search by name or MRN..."
        className="w-full rounded-lg border px-3 py-2 text-sm mb-4"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
      />

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-gray-500">
              <th className="pb-2">Name</th>
              <th className="pb-2">MRN</th>
              <th className="pb-2">DOB</th>
              <th className="pb-2">Gender</th>
              <th className="pb-2">Actions</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((p) => (
              <tr key={p.id} className="border-t">
                <td className="py-2">
                  {p.last_name && p.first_name
                    ? `${p.last_name}, ${p.first_name}`
                    : p.first_name || p.last_name || "\u2014"}
                </td>
                <td className="py-2">{p.mrn}</td>
                <td className="py-2">
                  {p.birth_date
                    ? new Date(p.birth_date).toLocaleDateString()
                    : "\u2014"}
                </td>
                <td className="py-2">{p.gender || "\u2014"}</td>
                <td className="py-2 flex gap-1">
                  <button
                    onClick={() => startEdit(p)}
                    className="px-2 py-1 border rounded text-xs hover:bg-gray-50"
                  >
                    Edit
                  </button>
                  <button
                    onClick={() => {
                      setSelected(p);
                      handleDelete();
                    }}
                    className="px-2 py-1 border border-red-200 text-red-600 rounded text-xs hover:bg-red-50"
                  >
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
