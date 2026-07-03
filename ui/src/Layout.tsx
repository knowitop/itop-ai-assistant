import { ActionIcon, Alert, Anchor, AppShell, Badge, Group, NavLink, Title, Tooltip } from '@mantine/core';
import { useEffect, useState } from 'react';
import { Link, Outlet, useLocation } from 'react-router-dom';

import { fetchHealth, fetchSetupStatus, Health, SetupStatus } from './api';

const NAV = [
  { to: '/setup', label: 'Setup' },
  { to: '/connections', label: 'Connections' },
  { to: '/modules', label: 'Modules' },
  { to: '/prompts', label: 'Prompts' },
  { to: '/runs', label: 'Runs' },
];

const REPO_URL = 'https://github.com/knowitop/itop-ai-assistant';

// Inline SVG keeps the "minimal dependencies" rule — no icon library.
const GithubIcon = () => (
  <svg viewBox="0 0 16 16" width={18} height={18} fill="currentColor" aria-hidden>
    <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0016 8c0-4.42-3.58-8-8-8z" />
  </svg>
);

export default function Layout() {
  const location = useLocation();
  const [health, setHealth] = useState<Health | null>(null);
  const [setup, setSetup] = useState<SetupStatus | null>(null);

  // Refetched on every navigation so the badges reflect wizard progress.
  useEffect(() => {
    fetchHealth().then(setHealth).catch(() => setHealth(null));
    fetchSetupStatus().then(setSetup).catch(() => setSetup(null));
  }, [location.pathname]);

  return (
    <AppShell header={{ height: 56 }} navbar={{ width: 220, breakpoint: 'sm' }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Title order={4}>iTop AI Assistant</Title>
          <Group gap="xs">
            {health ? (
              <Badge color={health.redis ? 'green' : 'yellow'} variant="light">
                {health.redis ? 'redis ok' : 'redis degraded'}
              </Badge>
            ) : (
              <Badge color="red" variant="light">
                offline
              </Badge>
            )}
            {setup && (
              <Badge color={setup.configured ? 'green' : 'orange'} variant="light">
                {setup.configured ? 'configured' : 'setup required'}
              </Badge>
            )}
            <Tooltip label="GitHub repository">
              <ActionIcon
                component="a"
                href={REPO_URL}
                target="_blank"
                rel="noopener noreferrer"
                variant="subtle"
                color="gray"
                aria-label="GitHub repository"
              >
                <GithubIcon />
              </ActionIcon>
            </Tooltip>
          </Group>
        </Group>
      </AppShell.Header>
      <AppShell.Navbar p="xs">
        {NAV.map((item) => (
          <NavLink
            key={item.to}
            component={Link}
            to={item.to}
            label={item.label}
            active={location.pathname.startsWith(item.to)}
          />
        ))}
      </AppShell.Navbar>
      <AppShell.Main>
        {setup && !setup.configured && location.pathname !== '/setup' && (
          <Alert color="orange" mb="md" title="Setup required">
            The assistant is not configured yet — /webhook returns 503 until the LLM and iTop
            connections are set.{' '}
            <Anchor component={Link} to="/setup">
              Run the setup wizard
            </Anchor>
            .
          </Alert>
        )}
        <Outlet />
      </AppShell.Main>
    </AppShell>
  );
}
