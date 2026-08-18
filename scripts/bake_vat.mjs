#!/usr/bin/env node
// Офлайн-бейкер VAT (Vertex Animation Texture) для толпы "Кач-Отряд" / "Кач-Раннер".
//
// Печёт скелетный клип из .glb (Mixamo/Adobe rig) в компактные файлы для клиента:
//   - <slug>_geom.bin   — статичная геометрия (позиции покоя + нормали + индексы),
//                         десятки КБ, скелет и джойнты в неё не попадают.
//   - <slug>_vat.bin    — VAT-текстура, RGBA8 (квантованное СМЕЩЕНИЕ от позы покоя),
//                         все запечённые клипы уложены друг под другом по высоте.
//   - <slug>_vat.json   — метаданные: число вершин/индексов, раскладка клипов по
//                         кадрам, диапазоны min/max для деквантования, ориентация.
//
// Почему офлайн, а не в браузере (как в прототипе proto_crowd.html):
//   - клиенту не нужно тащить 2+ МБ исходного .glb ради голой геометрии;
//   - VAT печётся в RGBA8 (квантованный), а не RGBA32F — в 4 раза меньше и не
//     требует float-текстур на телефонах;
//   - в проде это mini-app в Telegram — экономия трафика на старте имеет значение.
//
// Почему смещение от позы покоя, а не абсолютная позиция:
//   Диапазон абсолютных мировых координат тела ~2 м по Y (рост персонажа), при
//   8 битах на компоненту это ~8 мм/шаг — на глаз уже видно дрожание кистей/стоп.
//   Диапазон СМЕЩЕНИЯ от позы покоя за цикл бега на порядок меньше (конечности
//   отклоняются от T/A-позы на десятки см, а не на метры) — тот же 8-битный шаг
//   даёт на порядок точнее реконструкцию. Метаданные хранят min/max самого
//   смещения, геометрия — саму позу покоя, из которой оно отсчитывается.
//
// Важная особенность three.js, из-за которой легко получить кашу из вершин:
// SkinnedMesh.applyBoneTransform(index, vector) ПИШЕТ результат в vector, а не
// читает исходную позицию сам — вектор нужно заполнить исходной позицией вершины
// ДО вызова (см. bakeMeshFrame ниже).
//
// Запуск:
//   cd scripts && npm install   # один раз, тянет three.js (не коммитится)
//   node bake_vat.mjs ../../<где лежит>Soldier.glb soldier --clip Run:24 --clip Idle:16
//
// (для этой сессии агента исходники .glb лежат в scratchpad, не в репозитории —
// см. отчёт агента; путь передаётся первым аргументом).

import fs from 'node:fs';
import path from 'node:path';
import * as THREE from 'three';

// GLTFLoader's texture-decoding path expects a browser-ish global `self`.
// We don't need textures/materials for VAT baking (only geometry+skin+anim),
// but the loader still tries and fails without this — harmless no-op shim.
globalThis.self = globalThis;

const { GLTFLoader } = await import('three/examples/jsm/loaders/GLTFLoader.js');

function parseArgs(argv) {
  const [inputGlb, slug, ...rest] = argv;
  if (!inputGlb || !slug) {
    console.error('Usage: node bake_vat.mjs <input.glb> <slug> [--clip Name:frames ...] [--outdir DIR]');
    process.exit(1);
  }
  const clips = [];
  let outdir = path.resolve(new URL('.', import.meta.url).pathname, '../assets/game');
  for (let i = 0; i < rest.length; i++) {
    if (rest[i] === '--clip') {
      const [name, frames] = rest[++i].split(':');
      clips.push({ name, frames: parseInt(frames || '24', 10) });
    } else if (rest[i] === '--outdir') {
      outdir = path.resolve(rest[++i]);
    }
  }
  if (clips.length === 0) clips.push({ name: 'Run', frames: 24 });
  return { inputGlb, slug, clips, outdir };
}

function loadGltf(glbPath) {
  return new Promise((resolve, reject) => {
    const buf = fs.readFileSync(glbPath);
    const ab = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
    new GLTFLoader().parse(ab, '', resolve, reject);
  });
}

function collectSkinnedMeshes(root) {
  const meshes = [];
  root.traverse((o) => { if (o.isSkinnedMesh) meshes.push(o); });
  // detach from any group scaling weirdness: ensure world matrices are current
  meshes.sort((a, b) => a.name.localeCompare(b.name));
  return meshes;
}

// Bakes one frame's world-space positions+normals for one skinned mesh into
// flat Float32Arrays at the given vertex offset (in the combined buffer).
function bakeMeshFrame(mesh, outPos, outNormal, vertOffset) {
  const geom = mesh.geometry;
  const posAttr = geom.attributes.position;
  const normAttr = geom.attributes.normal;
  const numVerts = posAttr.count;
  const tmpV = new THREE.Vector3();
  const tmpN = new THREE.Vector3();
  const normalMat = new THREE.Matrix3().getNormalMatrix(mesh.matrixWorld);
  for (let v = 0; v < numVerts; v++) {
    tmpV.fromBufferAttribute(posAttr, v);
    mesh.applyBoneTransform(v, tmpV); // writes INTO tmpV — must be pre-filled above
    tmpV.applyMatrix4(mesh.matrixWorld);
    const o = (vertOffset + v) * 3;
    outPos[o] = tmpV.x; outPos[o + 1] = tmpV.y; outPos[o + 2] = tmpV.z;

    if (normAttr) {
      tmpN.fromBufferAttribute(normAttr, v).applyMatrix3(normalMat).normalize();
      outNormal[o] = tmpN.x; outNormal[o + 1] = tmpN.y; outNormal[o + 2] = tmpN.z;
    }
  }
}

async function main() {
  const { inputGlb, slug, clips, outdir } = parseArgs(process.argv.slice(2));
  fs.mkdirSync(outdir, { recursive: true });

  console.log(`[bake] loading ${inputGlb} ...`);
  const gltf = await loadGltf(inputGlb);
  const root = gltf.scene;
  root.updateMatrixWorld(true);

  const meshes = collectSkinnedMeshes(root);
  if (meshes.length === 0) throw new Error('No SkinnedMesh found in ' + inputGlb);
  console.log(`[bake] skinned meshes: ${meshes.map((m) => `${m.name}(${m.geometry.attributes.position.count}v)`).join(', ')}`);

  // Combined vertex layout: meshes concatenated in name order.
  let numVerts = 0;
  const vertOffsets = [];
  for (const m of meshes) { vertOffsets.push(numVerts); numVerts += m.geometry.attributes.position.count; }

  // Combined index buffer (uint16 fits — Soldier.glb has 7434 verts total).
  let numIndices = 0;
  for (const m of meshes) numIndices += (m.geometry.index ? m.geometry.index.count : m.geometry.attributes.position.count);
  if (numVerts > 65535) throw new Error(`numVerts=${numVerts} exceeds uint16 range; switch indices to uint32`);
  const indices = new Uint16Array(numIndices);
  {
    let iOff = 0;
    for (let mi = 0; mi < meshes.length; mi++) {
      const g = meshes[mi].geometry;
      const idx = g.index ? g.index.array : null;
      const cnt = g.index ? g.index.count : g.attributes.position.count;
      for (let k = 0; k < cnt; k++) indices[iOff + k] = (idx ? idx[k] : k) + vertOffsets[mi];
      iOff += cnt;
    }
  }

  // --- Rest pose (bind pose) in world space -> becomes the static geometry ---
  for (const m of meshes) m.skeleton.pose();
  root.updateMatrixWorld(true);
  for (const m of meshes) m.skeleton.update();
  const restPos = new Float32Array(numVerts * 3);
  const restNormal = new Float32Array(numVerts * 3);
  for (let mi = 0; mi < meshes.length; mi++) bakeMeshFrame(meshes[mi], restPos, restNormal, vertOffsets[mi]);

  // --- Bake each requested clip, sampling FRAMES steps across its duration ---
  const clipMeta = [];
  const perClipDeltas = [];
  let totalFrames = 0;
  const mixer = new THREE.AnimationMixer(root);

  for (const { name, frames } of clips) {
    const clip = THREE.AnimationClip.findByName(gltf.animations, name);
    if (!clip) { console.warn(`[bake] clip "${name}" not found, skipping. Available: ${gltf.animations.map(a=>a.name).join(', ')}`); continue; }
    const action = mixer.clipAction(clip);
    action.play();
    action.paused = true;

    const deltas = new Float32Array(numVerts * frames * 3);
    const framePos = new Float32Array(numVerts * 3);
    const centroidX = [], centroidZ = [];

    for (let f = 0; f < frames; f++) {
      const t = (f / frames) * clip.duration;
      mixer.setTime(t);
      root.updateMatrixWorld(true);
      for (const m of meshes) m.skeleton.update();
      for (let mi = 0; mi < meshes.length; mi++) bakeMeshFrame(meshes[mi], framePos, restNormal /* scratch, unused here */, vertOffsets[mi]);

      let cx = 0, cz = 0;
      for (let v = 0; v < numVerts; v++) {
        const o = v * 3;
        deltas[(f * numVerts + v) * 3] = framePos[o] - restPos[o];
        deltas[(f * numVerts + v) * 3 + 1] = framePos[o + 1] - restPos[o + 1];
        deltas[(f * numVerts + v) * 3 + 2] = framePos[o + 2] - restPos[o + 2];
        cx += framePos[o]; cz += framePos[o + 2];
      }
      centroidX.push(cx / numVerts); centroidZ.push(cz / numVerts);
    }
    action.stop();
    mixer.uncacheAction(clip, root);

    const driftX = Math.max(...centroidX) - Math.min(...centroidX);
    const driftZ = Math.max(...centroidZ) - Math.min(...centroidZ);
    console.log(`[bake] clip "${name}": ${frames} frames, duration ${clip.duration.toFixed(3)}s, XZ centroid drift over loop: dx=${driftX.toFixed(4)}m dz=${driftZ.toFixed(4)}m ${(driftX>0.05||driftZ>0.05) ? '<-- root motion baked in, crowd will drift per loop!' : '(in-place, safe to loop)'}`);

    clipMeta.push({ clip: name, frameStart: totalFrames, frameCount: frames, duration: clip.duration });
    perClipDeltas.push(deltas);
    totalFrames += frames;
  }

  // --- Quantize: per-axis min/max across ALL baked frames of ALL clips ---
  const mn = [Infinity, Infinity, Infinity];
  const mx = [-Infinity, -Infinity, -Infinity];
  for (const deltas of perClipDeltas) {
    for (let i = 0; i < deltas.length; i += 3) {
      for (let c = 0; c < 3; c++) { const d = deltas[i + c]; if (d < mn[c]) mn[c] = d; if (d > mx[c]) mx[c] = d; }
    }
  }
  console.log(`[bake] delta range: min=${mn.map(x=>x.toFixed(4))} max=${mx.map(x=>x.toFixed(4))}`);

  const vat = new Uint8Array(numVerts * totalFrames * 4);
  {
    let rowOffset = 0;
    for (const deltas of perClipDeltas) {
      const frames = deltas.length / (numVerts * 3);
      for (let f = 0; f < frames; f++) {
        for (let v = 0; v < numVerts; v++) {
          const si = (f * numVerts + v) * 3;
          const di = ((rowOffset + f) * numVerts + v) * 4;
          for (let c = 0; c < 3; c++) {
            const d = deltas[si + c];
            const t01 = mx[c] > mn[c] ? (d - mn[c]) / (mx[c] - mn[c]) : 0;
            vat[di + c] = Math.max(0, Math.min(255, Math.round(t01 * 255)));
          }
          vat[di + 3] = 255;
        }
      }
      rowOffset += frames;
    }
  }

  // --- Write files (flat directory — server route rejects "/" in filenames) ---
  const geomPath = path.join(outdir, `${slug}_geom.bin`);
  const vatPath = path.join(outdir, `${slug}_vat.bin`);
  const jsonPath = path.join(outdir, `${slug}_vat.json`);

  const geomBuf = Buffer.concat([Buffer.from(restPos.buffer), Buffer.from(restNormal.buffer), Buffer.from(indices.buffer)]);
  fs.writeFileSync(geomPath, geomBuf);
  fs.writeFileSync(vatPath, Buffer.from(vat.buffer));

  const meta = {
    sourceFile: path.basename(inputGlb),
    generatedAt: new Date().toISOString(),
    numVerts,
    numIndices,
    indexType: 'uint16',
    geom: { positionsBytes: restPos.byteLength, normalsBytes: restNormal.byteLength, indicesBytes: indices.byteLength, layout: ['positions f32 xyz', 'normals f32 xyz', 'indices u16'] },
    vat: {
      textureWidth: numVerts,
      textureHeight: totalFrames,
      format: 'RGBA8',
      encoding: 'delta-from-rest-pose, quantized per-axis to 0..255',
      quantMin: mn,
      quantMax: mx,
      clips: clipMeta,
    },
    // world-space note: baked positions already include the source root node's
    // transform (Soldier.glb root "Character": scale 0.01, rotation -90deg X) —
    // consumers place instances directly in world units, no extra scale/rotate.
    rootTransformBakedIn: true,
  };
  fs.writeFileSync(jsonPath, JSON.stringify(meta, null, 2));

  const totalBytes = geomBuf.byteLength + vat.byteLength + fs.statSync(jsonPath).size;
  console.log(`[bake] wrote:`);
  console.log(`  ${geomPath}  (${(geomBuf.byteLength/1024).toFixed(1)} KB)`);
  console.log(`  ${vatPath}  (${(vat.byteLength/1024).toFixed(1)} KB)  [float32 equivalent would be ${(numVerts*totalFrames*16/1024).toFixed(1)} KB]`);
  console.log(`  ${jsonPath}  (${(fs.statSync(jsonPath).size/1024).toFixed(2)} KB)`);
  console.log(`[bake] total: ${(totalBytes/1024).toFixed(1)} KB`);
}

main().catch((e) => { console.error(e); process.exit(1); });
