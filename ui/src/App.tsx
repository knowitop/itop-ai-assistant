import { Button, Center, MantineProvider, PasswordInput, Stack, Text, Title } from '@mantine/core';
import { useEffect, useState } from 'react';
import { HashRouter, Navigate, Route, Routes } from 'react-router-dom';

import { setToken, setUnauthorizedHandler } from './api';
import Connections from './Connections';
import Layout from './Layout';
import Modules from './Modules';
import Prompts from './Prompts';
import Runs from './Runs';
import SetupWizard from './SetupWizard';

// HashRouter keeps all routes under /ui/#/... so FastAPI StaticFiles can serve
// the SPA without a server-side fallback for deep links.
export default function App() {
  const [needToken, setNeedToken] = useState(false);

  useEffect(() => {
    setUnauthorizedHandler(() => setNeedToken(true));
  }, []);

  if (needToken) {
    return (
      <MantineProvider>
        <TokenGate />
      </MantineProvider>
    );
  }

  return (
    <MantineProvider>
      <HashRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<Navigate to="/setup" replace />} />
            <Route path="/setup" element={<SetupWizard />} />
            <Route path="/connections" element={<Connections />} />
            <Route path="/modules" element={<Modules />} />
            <Route path="/prompts" element={<Prompts />} />
            <Route path="/runs" element={<Runs />} />
          </Route>
        </Routes>
      </HashRouter>
    </MantineProvider>
  );
}

function TokenGate() {
  const [value, setValue] = useState('');

  const save = () => {
    setToken(value.trim());
    // Reload so every screen refetches with the new token.
    window.location.reload();
  };

  return (
    <Center h="100vh">
      <Stack w={360}>
        <Title order={3}>iTop AI Assistant</Title>
        <Text c="dimmed">The admin API rejected the request. Enter the admin token to continue.</Text>
        <PasswordInput
          label="Admin token"
          value={value}
          onChange={(event) => setValue(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && value.trim()) save();
          }}
          data-autofocus
        />
        <Button onClick={save} disabled={!value.trim()}>
          Save token
        </Button>
      </Stack>
    </Center>
  );
}
