"""Exercise the actual TypeScript demo with in-process fake fetch, never cloud network."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

class TtsReceiptTests(unittest.TestCase):
    def test_clone_only_records_identity_without_synthesis(self):
        script=Path.home()/'.codex/skills/dashscope-voice-clone-tts/scripts/audio-clone-demo.ts'
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp)
            (d/'mock.mjs').write_text('''globalThis.fetch=async (url,options)=>{
if (!String(url).endsWith('/customization')) throw new Error('Synthesis or network forbidden');
const b=JSON.parse(options.body); if(b.input.action!=='create_voice')throw new Error('Wrong action');
return new Response(JSON.stringify({request_id:'fixture_clone',output:{voice_id:'fixture_voice'}}));};''')
            env={**os.environ,'DASHSCOPE_API_KEY':'fixture','CLONE_ONLY':'1','VOICE_ID':'',
                 'REFERENCE_AUDIO_URL':'https://fixture.invalid/reference.wav',
                 'RECEIPT_FILE':str(d/'clone.json')}
            r=subprocess.run(['node','--experimental-strip-types','--import',str(d/'mock.mjs'),str(script)],
                             env=env,cwd=d,capture_output=True,text=True,timeout=15)
            self.assertEqual(r.returncode,0,r.stderr)
            self.assertEqual(json.loads((d/'clone.json').read_text())['voice_id'],'fixture_voice')
            self.assertFalse((d/'cloned-audio.mp3').exists())
    def test_exact_text_and_receipt_preflight(self):
        script=Path.home()/'.codex/skills/dashscope-voice-clone-tts/scripts/audio-clone-demo.ts'
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp);text=' Hello & goodbye. '\
                '\n'
            (d/'text.txt').write_text(text)
            (d/'mock.mjs').write_text('''import { readFileSync } from 'node:fs';
globalThis.fetch=async (url,options)=>{
  if (String(url).endsWith('/SpeechSynthesizer')) {
    const b=JSON.parse(options.body);
    if(b.input.text!==readFileSync(process.env.CLONE_TEXT_FILE,'utf8')) throw new Error('text changed');
    return new Response(JSON.stringify({request_id:'fixture_request',output:{audio:{url:'https://fixture.invalid/audio.mp3',duration:1}}}));
  }
  if(String(url)==='https://fixture.invalid/audio.mp3')return new Response(new Uint8Array([1,2,3,4]));
  throw new Error('Unexpected URL; live network disabled');
};''')
            env={**os.environ,'DASHSCOPE_API_KEY':'fixture_key','VOICE_ID':'fixture_voice',
                 'CLONE_TEXT_FILE':str(d/'text.txt'),'OUTPUT_FILE':str(d/'audio.mp3'),
                 'RECEIPT_FILE':str(d/'receipt.json')}
            args=['node','--experimental-strip-types','--import',str(d/'mock.mjs'),str(script)]
            r=subprocess.run(args,env=env,capture_output=True,text=True,timeout=15)
            self.assertEqual(r.returncode,0,r.stderr)
            receipt=json.loads((d/'receipt.json').read_text())
            self.assertEqual(receipt['request_id'],'fixture_request')
            self.assertEqual(receipt['text_sha256'],hashlib.sha256(text.encode()).hexdigest())
            self.assertEqual(receipt['audio_sha256'],hashlib.sha256(bytes([1,2,3,4])).hexdigest())
            before=(d/'audio.mp3').read_bytes()
            r=subprocess.run(args,env=env,capture_output=True,text=True,timeout=15)
            self.assertNotEqual(r.returncode,0)
            self.assertEqual((d/'audio.mp3').read_bytes(),before)

if __name__=='__main__':unittest.main()
