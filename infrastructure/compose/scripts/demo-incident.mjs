// The browser demo replays a recorded report for one bundled configuration fixture.
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';

const { context, 'root-cause': example } = JSON.parse(await readFile('/demo/case.json', 'utf8'));

if (process.argv.includes('--seed')) {
  const token = (await readFile('/run/rcannon-secrets/rcannon_operator_bearer_token', 'utf8')).trim();
  const response = await fetch('http://rcannon:8000/api/v1/snapshots', {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ bundle: { source_type: 'fixture', context } }),
    signal: AbortSignal.timeout(10000),
  });
  if (!response.ok) throw new Error(`Could not load the sample incident (HTTP ${response.status}).`);
  const { snapshot_id: snapshotId } = await response.json();
  if (!/^snap_[a-f0-9]+$/.test(snapshotId)) throw new Error('The sample incident has no snapshot ID.');
  process.stdout.write(`/rca snapshot=${snapshotId}\n`);
} else {
  const assessment = 'Recorded demo response. The fixture supplies configuration evidence.';
  // Keep the expected report separate from the evidence sent to RCAnnon.
  const report = {
    ...example,
    cause: `Recorded demo report. ${example.cause}`,
    supporting_evidence: [
      'The Deployment/api manifest sets the web container memory limit to 64Mi.',
      'The same container sets ALLOCATE_MIB to 128.',
    ],
    remediation: 'Raise the web container memory limit to 256Mi to accommodate its working set and runtime overhead.',
    recovery_check: 'Confirm that the container reaches readiness and remains running without further memory-related restarts.',
  };

  function completionFor(payload) {
    if ('context' in payload) return report;
    if ('logs' in payload) {
      return {
        agent: 'logs-connectivity', log_findings: [], connectivity_findings: [],
        runtime_failure_mode: 'unknown', preliminary_assessment: assessment,
      };
    }
    if ('metrics' in payload) {
      return {
        agent: 'metrics-telemetry', resource_findings: [], throttling_detected: false,
        saturation_identified: null, preliminary_assessment: assessment,
      };
    }
    return {
      agent: 'k8s-config-events', anomalies: [], suspicious_resources: [],
      significant_events: [], preliminary_assessment: assessment,
    };
  }

  createServer(async (request, response) => {
    if (request.url === '/healthz') {
      response.writeHead(200).end();
      return;
    }
    if (request.method !== 'POST' || request.url !== '/v1/chat/completions') {
      response.writeHead(404).end();
      return;
    }
    response.setHeader('Content-Type', 'application/json');
    try {
      const chunks = [];
      let bytes = 0;
      for await (const chunk of request) {
        bytes += chunk.length;
        if (bytes > 1024 * 1024) throw new Error('Demo request too large.');
        chunks.push(chunk);
      }
      const body = JSON.parse(Buffer.concat(chunks));
      const payload = JSON.parse(body.messages.at(-1).content);
      response.end(JSON.stringify({
        id: 'recorded-incident',
        object: 'chat.completion',
        created: 0,
        model: 'recorded-incident',
        choices: [{
          index: 0, finish_reason: 'stop',
          message: { role: 'assistant', content: JSON.stringify(completionFor(payload)) },
        }],
      }));
    } catch {
      response.writeHead(400).end(JSON.stringify({ error: { message: 'Invalid demo request.' } }));
    }
  }).listen(8081, '0.0.0.0');
}
