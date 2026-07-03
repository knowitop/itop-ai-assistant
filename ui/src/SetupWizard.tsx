import { Alert, Code, List, Loader, Stack, Text, Title } from '@mantine/core';
import { useEffect, useState } from 'react';

import { fetchSetupStatus, SetupStatus } from './api';

// Placeholder: shows the raw setup status. The step-by-step wizard is plan step 2.
export default function SetupWizard() {
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchSetupStatus()
      .then(setStatus)
      .catch((e: Error) => setError(e.message));
  }, []);

  return (
    <Stack>
      <Title order={2}>Setup</Title>
      {error && <Alert color="red">{error}</Alert>}
      {!status && !error && <Loader />}
      {status && (
        <>
          <Text>
            Status: <Code>{status.configured ? 'configured' : 'not configured'}</Code>
          </Text>
          {status.missing.length > 0 && (
            <>
              <Text>Missing settings:</Text>
              <List>
                {status.missing.map((item) => (
                  <List.Item key={item}>{item}</List.Item>
                ))}
              </List>
            </>
          )}
          <Text c="dimmed">The setup wizard will appear here (plan step 2).</Text>
        </>
      )}
    </Stack>
  );
}
