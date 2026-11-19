import {
  ActionIcon,
  AppShell,
  Burger,
  Center,
  Divider,
  Group,
  Loader,
  NavLink,
  Text,
  Title,
  Tooltip,
  useComputedColorScheme,
  useMantineColorScheme,
} from '@mantine/core'
import { useDisclosure } from '@mantine/hooks'
import {
  IconActivity,
  IconBuildingSkyscraper,
  IconLogout,
  IconMoon,
  IconPlayerPlay,
  IconReportMoney,
  IconSun,
  IconTarget,
  IconUsers,
} from '@tabler/icons-react'
import { useQueryClient } from '@tanstack/react-query'
import { lazy, Suspense, useEffect } from 'react'
import { Link, Navigate, Route, Routes, useLocation, useParams } from 'react-router'
import { api } from './api/client'
import { LoginPage } from './auth'
import { PersonSearch } from './components/PersonSearch'
import { useMe, useWords } from './lib'
import { DirectoryPage } from './pages/Directory'
import { OrganisationsPage } from './pages/Organisations'
import { MovesPage } from './pages/Moves'
import { PersonPage } from './pages/Person'

// The admin pages pull in the charting library; load them only for admins who open them.
const AdminPage = lazy(() => import('./pages/Admin').then((m) => ({ default: m.AdminPage })))
const TrackingPage = lazy(() => import('./pages/Tracking').then((m) => ({ default: m.TrackingPage })))
const WorkerPage = lazy(() => import('./pages/Worker').then((m) => ({ default: m.WorkerPage })))

// Links from before /partners became /people keep working.
function OldPersonLink() {
  return <Navigate to={`/people/${useParams().id}`} replace />
}

export function App() {
  const me = useMe()
  if (me.isPending) {
    return (
      <Center h="100vh">
        <Loader />
      </Center>
    )
  }
  if (!me.data) return <LoginPage />
  return <Shell isAdmin={me.data.role === 'admin'} email={me.data.email} />
}

function Shell({ isAdmin, email }: { isAdmin: boolean; email: string }) {
  const [opened, { toggle, close }] = useDisclosure()
  const location = useLocation()
  const queryClient = useQueryClient()
  const { setColorScheme } = useMantineColorScheme()
  const scheme = useComputedColorScheme('light')
  const words = useWords()
  useEffect(() => {
    document.title = words.app_name
  }, [words.app_name])

  const logout = async () => {
    await api.POST('/api/auth/logout')
    queryClient.clear()
  }
  const nav = [
    { to: '/', label: 'Latest moves', icon: IconActivity },
    { to: '/people', label: words.people, icon: IconUsers },
    { to: '/organisations', label: words.orgs, icon: IconBuildingSkyscraper },
  ]
  const admin = [
    { to: '/tracking', label: 'What we track', icon: IconTarget },
    { to: '/admin', label: 'AI & costs', icon: IconReportMoney },
    { to: '/worker', label: 'Worker', icon: IconPlayerPlay },
  ]
  const link = (item: (typeof nav)[number]) => (
    <NavLink
      key={item.to}
      component={Link}
      to={item.to}
      label={item.label}
      leftSection={<item.icon size={18} />}
      active={item.to === '/' ? location.pathname === '/' : location.pathname.startsWith(item.to)}
      onClick={close}
    />
  )

  return (
    <AppShell header={{ height: 56 }} navbar={{ width: 260, breakpoint: 'sm', collapsed: { mobile: !opened } }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group gap="sm">
            <Burger opened={opened} onClick={toggle} hiddenFrom="sm" size="sm" />
            <Title order={4} visibleFrom="xs">
              {words.app_name}
            </Title>
            <Text size="sm" c="dimmed" visibleFrom="lg">
              {words.tagline}
            </Text>
          </Group>
          <Group gap="xs" wrap="nowrap">
            <PersonSearch />
            <Tooltip label={`Signed in as ${email}`}>
              <Text size="sm" c="dimmed" visibleFrom="md" truncate maw={200}>
                {email}
              </Text>
            </Tooltip>
            <Tooltip label="Toggle dark mode">
              <ActionIcon variant="subtle" onClick={() => setColorScheme(scheme === 'dark' ? 'light' : 'dark')} aria-label="Toggle dark mode">
                {scheme === 'dark' ? <IconSun size={18} /> : <IconMoon size={18} />}
              </ActionIcon>
            </Tooltip>
            <Tooltip label="Sign out">
              <ActionIcon variant="subtle" onClick={logout} aria-label="Sign out">
                <IconLogout size={18} />
              </ActionIcon>
            </Tooltip>
          </Group>
        </Group>
      </AppShell.Header>
      <AppShell.Navbar p="xs">
        {nav.map(link)}
        {isAdmin && (
          <>
            <Divider my="sm" label="Admin" labelPosition="left" />
            {admin.map(link)}
          </>
        )}
      </AppShell.Navbar>
      <AppShell.Main>
        <Routes>
          <Route path="/" element={<MovesPage />} />
          <Route path="/people" element={<DirectoryPage />} />
          <Route path="/people/:id" element={<PersonPage />} />
          <Route path="/partners" element={<Navigate to="/people" replace />} />
          <Route path="/partners/:id" element={<OldPersonLink />} />
          <Route path="/organisations" element={<OrganisationsPage isAdmin={isAdmin} />} />
          {isAdmin && (
            <Route
              path="/tracking"
              element={
                <Suspense fallback={<Loader />}>
                  <TrackingPage />
                </Suspense>
              }
            />
          )}
          {isAdmin && (
            <Route
              path="/admin"
              element={
                <Suspense fallback={<Loader />}>
                  <AdminPage />
                </Suspense>
              }
            />
          )}
          {isAdmin && (
            <Route
              path="/worker"
              element={
                <Suspense fallback={<Loader />}>
                  <WorkerPage />
                </Suspense>
              }
            />
          )}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </AppShell.Main>
    </AppShell>
  )
}
