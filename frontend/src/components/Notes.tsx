import { ActionIcon, Badge, Button, Checkbox, Group, Stack, Text, Textarea, Tooltip } from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { IconPencil, IconTrash } from '@tabler/icons-react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api, unwrap, type Schemas } from '../api/client'
import { formatDate } from '../lib'

type Note = Schemas['NoteOut']

/** Invalidate everything that shows a person's notes: their profile (whose AI summary may now be
 * out of date), the notes list on its own, and the People list's note counts. */
function useRefreshNotes(personId: string) {
  const queryClient = useQueryClient()
  return () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ['person', personId] }),
      queryClient.invalidateQueries({ queryKey: ['notes', personId] }),
      queryClient.invalidateQueries({ queryKey: ['people'] }),
    ])
}

/** Add, edit and delete a person's notes. Used on their profile and in the People list's drawer. */
export function NotesPanel({ personId, notes }: { personId: string; notes: Note[] }) {
  const refresh = useRefreshNotes(personId)
  const [text, setText] = useState('')
  const [useInBrief, setUseInBrief] = useState(true)
  const add = useMutation({
    mutationFn: () =>
      unwrap(api.POST('/api/people/{person_id}/notes', { params: { path: { person_id: personId } }, body: { text, use_in_brief: useInBrief } })),
    onSuccess: () => {
      setText('')
      setUseInBrief(true)
      void refresh()
    },
    onError: (error) => notifications.show({ color: 'red', title: 'Note not saved', message: error.message }),
  })
  return (
    <Stack gap="xs">
      <Textarea
        placeholder="What did you learn? e.g. spoke on 12 Nov, open to a move in spring"
        autosize
        minRows={2}
        maxLength={4000}
        value={text}
        onChange={(e) => setText(e.currentTarget.value)}
        onKeyDown={(e) => {
          // Ctrl/Cmd+Enter saves, like most chat and comment boxes.
          if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && text.trim()) add.mutate()
        }}
      />
      <Group justify="space-between">
        <BriefCheckbox checked={useInBrief} onChange={setUseInBrief} />
        <Button size="xs" loading={add.isPending} disabled={!text.trim()} onClick={() => add.mutate()}>
          Add note
        </Button>
      </Group>
      {notes.length === 0 && (
        <Text size="sm" c="dimmed">
          No notes yet. Notes are visible to everyone on the team.
        </Text>
      )}
      {notes.map((note) => (
        <NoteItem key={note.id} note={note} personId={personId} />
      ))}
    </Stack>
  )
}

function BriefCheckbox({ checked, onChange }: { checked: boolean; onChange: (value: boolean) => void }) {
  return (
    <Tooltip multiline w={280} label="Untick for anything sensitive. The note is still shown to colleagues, but not sent to the AI model.">
      <Checkbox size="xs" label="Use in AI summary" checked={checked} onChange={(e) => onChange(e.currentTarget.checked)} />
    </Tooltip>
  )
}

function NoteItem({ note, personId }: { note: Note; personId: string }) {
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(note.text)
  const [useInBrief, setUseInBrief] = useState(note.use_in_brief)
  const refresh = useRefreshNotes(personId)
  const save = useMutation({
    mutationFn: () => unwrap(api.PATCH('/api/notes/{note_id}', { params: { path: { note_id: note.id } }, body: { text, use_in_brief: useInBrief } })),
    onSuccess: () => {
      setEditing(false)
      void refresh()
    },
    onError: (error) => notifications.show({ color: 'red', title: 'Note not saved', message: error.message }),
  })
  const remove = useMutation({
    mutationFn: () => unwrap(api.DELETE('/api/notes/{note_id}', { params: { path: { note_id: note.id } } })),
    onSuccess: () => void refresh(),
    onError: (error) => notifications.show({ color: 'red', title: 'Note not deleted', message: error.message }),
  })

  if (editing) {
    return (
      <Stack gap={4}>
        <Textarea autosize minRows={2} maxLength={4000} value={text} onChange={(e) => setText(e.currentTarget.value)} />
        <Group justify="space-between">
          <BriefCheckbox checked={useInBrief} onChange={setUseInBrief} />
          <Group gap="xs">
            <Button size="xs" variant="default" onClick={() => setEditing(false)}>
              Cancel
            </Button>
            <Button size="xs" loading={save.isPending} disabled={!text.trim()} onClick={() => save.mutate()}>
              Save
            </Button>
          </Group>
        </Group>
      </Stack>
    )
  }
  return (
    <Group align="start" wrap="nowrap" gap="xs" pt={6} style={{ borderTop: '1px solid var(--mantine-color-default-border)' }}>
      <div style={{ flex: 1 }}>
        <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
          {note.text}
        </Text>
        <Group gap={6}>
          <Text size="xs" c="dimmed">
            {note.author} · {formatDate(note.created_at)}
            {note.updated_at ? ' (edited)' : ''}
          </Text>
          {!note.use_in_brief && (
            <Badge size="xs" variant="light" color="gray">
              Not used by AI
            </Badge>
          )}
        </Group>
      </div>
      {note.can_edit && (
        <Group gap={2} wrap="nowrap">
          <ActionIcon
            variant="subtle"
            size="sm"
            aria-label="Edit note"
            onClick={() => {
              setText(note.text)
              setUseInBrief(note.use_in_brief)
              setEditing(true)
            }}
          >
            <IconPencil size={14} />
          </ActionIcon>
          <ActionIcon
            variant="subtle"
            color="red"
            size="sm"
            aria-label="Delete note"
            loading={remove.isPending}
            onClick={() => {
              if (window.confirm('Delete this note?')) remove.mutate()
            }}
          >
            <IconTrash size={14} />
          </ActionIcon>
        </Group>
      )}
    </Group>
  )
}
