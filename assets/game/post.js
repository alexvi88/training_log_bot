(()=>{var Z=window.THREE;var{ACESFilmicToneMapping:D,AddEquation:$,AddOperation:ee,AdditiveAnimationBlendMode:te,AdditiveBlending:E,AgXToneMapping:G,AlphaFormat:re,AlwaysCompare:ie,AlwaysDepth:ae,AlwaysStencilFunc:se,AmbientLight:oe,AnimationAction:le,AnimationClip:ne,AnimationLoader:ue,AnimationMixer:he,AnimationObjectGroup:fe,AnimationUtils:pe,ArcCurve:me,ArrayCamera:ce,ArrowHelper:de,AttachedBindMode:ge,Audio:Ce,AudioAnalyser:Te,AudioContext:Se,AudioListener:ve,AudioLoader:Me,AxesHelper:_e,BackSide:xe,BasicDepthPacking:be,BasicShadowMap:Re,BatchedMesh:Ae,Bone:Be,BooleanKeyframeTrack:Fe,Box2:ye,Box3:Pe,Box3Helper:Le,BoxGeometry:De,BoxHelper:Ee,BufferAttribute:Ge,BufferGeometry:w,BufferGeometryLoader:we,ByteType:Ue,Cache:Ne,Camera:Ie,CameraHelper:Oe,CanvasTexture:He,CapsuleGeometry:Ve,CatmullRomCurve3:ze,CineonToneMapping:U,CircleGeometry:Qe,ClampToEdgeWrapping:ke,Clock:N,Color:c,ColorKeyframeTrack:We,ColorManagement:I,CompressedArrayTexture:qe,CompressedCubeTexture:Ke,CompressedTexture:je,CompressedTextureLoader:Xe,ConeGeometry:Ze,ConstantAlphaFactor:Ye,ConstantColorFactor:Je,CubeCamera:$e,CubeReflectionMapping:et,CubeRefractionMapping:tt,CubeTexture:rt,CubeTextureLoader:it,CubeUVReflectionMapping:at,CubicBezierCurve:st,CubicBezierCurve3:ot,CubicInterpolant:lt,CullFaceBack:nt,CullFaceFront:ut,CullFaceFrontBack:ht,CullFaceNone:ft,Curve:pt,CurvePath:mt,CustomBlending:ct,CustomToneMapping:dt,CylinderGeometry:gt,Cylindrical:Ct,Data3DTexture:Tt,DataArrayTexture:St,DataTexture:vt,DataTextureLoader:Mt,DataUtils:_t,DecrementStencilOp:xt,DecrementWrapStencilOp:bt,DefaultLoadingManager:Rt,DepthFormat:At,DepthStencilFormat:Bt,DepthTexture:Ft,DetachedBindMode:yt,DirectionalLight:Pt,DirectionalLightHelper:Lt,DiscreteInterpolant:Dt,DisplayP3ColorSpace:Et,DodecahedronGeometry:Gt,DoubleSide:wt,DstAlphaFactor:Ut,DstColorFactor:Nt,DynamicCopyUsage:It,DynamicDrawUsage:Ot,DynamicReadUsage:Ht,EdgesGeometry:Vt,EllipseCurve:zt,EqualCompare:Qt,EqualDepth:kt,EqualStencilFunc:Wt,EquirectangularReflectionMapping:qt,EquirectangularRefractionMapping:Kt,Euler:jt,EventDispatcher:Xt,ExtrudeGeometry:Zt,FileLoader:Yt,Float16BufferAttribute:Jt,Float32BufferAttribute:P,Float64BufferAttribute:$t,FloatType:er,Fog:tr,FogExp2:rr,FramebufferTexture:ir,FrontSide:ar,Frustum:sr,GLBufferAttribute:or,GLSL1:lr,GLSL3:nr,GreaterCompare:ur,GreaterDepth:hr,GreaterEqualCompare:fr,GreaterEqualDepth:pr,GreaterEqualStencilFunc:mr,GreaterStencilFunc:cr,GridHelper:dr,Group:gr,HalfFloatType:g,HemisphereLight:Cr,HemisphereLightHelper:Tr,IcosahedronGeometry:Sr,ImageBitmapLoader:vr,ImageLoader:Mr,ImageUtils:_r,IncrementStencilOp:xr,IncrementWrapStencilOp:br,InstancedBufferAttribute:Rr,InstancedBufferGeometry:Ar,InstancedInterleavedBuffer:Br,InstancedMesh:Fr,Int16BufferAttribute:yr,Int32BufferAttribute:Pr,Int8BufferAttribute:Lr,IntType:Dr,InterleavedBuffer:Er,InterleavedBufferAttribute:Gr,Interpolant:wr,InterpolateDiscrete:Ur,InterpolateLinear:Nr,InterpolateSmooth:Ir,InvertStencilOp:Or,KeepStencilOp:Hr,KeyframeTrack:Vr,LOD:zr,LatheGeometry:Qr,Layers:kr,LessCompare:Wr,LessDepth:qr,LessEqualCompare:Kr,LessEqualDepth:jr,LessEqualStencilFunc:Xr,LessStencilFunc:Zr,Light:Yr,LightProbe:Jr,Line:$r,Line3:ei,LineBasicMaterial:ti,LineCurve:ri,LineCurve3:ii,LineDashedMaterial:ai,LineLoop:si,LineSegments:oi,LinearDisplayP3ColorSpace:li,LinearEncoding:ni,LinearFilter:ui,LinearInterpolant:hi,LinearMipMapLinearFilter:fi,LinearMipMapNearestFilter:pi,LinearMipmapLinearFilter:mi,LinearMipmapNearestFilter:ci,LinearSRGBColorSpace:di,LinearToneMapping:O,LinearTransfer:gi,Loader:Ci,LoaderUtils:Ti,LoadingManager:Si,LoopOnce:vi,LoopPingPong:Mi,LoopRepeat:_i,LuminanceAlphaFormat:xi,LuminanceFormat:bi,MOUSE:Ri,Material:Ai,MaterialLoader:Bi,MathUtils:Fi,Matrix3:yi,Matrix4:Pi,MaxEquation:Li,Mesh:H,MeshBasicMaterial:V,MeshDepthMaterial:Di,MeshDistanceMaterial:Ei,MeshLambertMaterial:Gi,MeshMatcapMaterial:wi,MeshNormalMaterial:Ui,MeshPhongMaterial:Ni,MeshPhysicalMaterial:Ii,MeshStandardMaterial:Oi,MeshToonMaterial:Hi,MinEquation:Vi,MirroredRepeatWrapping:zi,MixOperation:Qi,MultiplyBlending:ki,MultiplyOperation:Wi,NearestFilter:qi,NearestMipMapLinearFilter:Ki,NearestMipMapNearestFilter:ji,NearestMipmapLinearFilter:Xi,NearestMipmapNearestFilter:Zi,NeverCompare:Yi,NeverDepth:Ji,NeverStencilFunc:$i,NoBlending:z,NoColorSpace:ea,NoToneMapping:ta,NormalAnimationBlendMode:ra,NormalBlending:ia,NotEqualCompare:aa,NotEqualDepth:sa,NotEqualStencilFunc:oa,NumberKeyframeTrack:la,Object3D:na,ObjectLoader:ua,ObjectSpaceNormalMap:ha,OctahedronGeometry:fa,OneFactor:pa,OneMinusConstantAlphaFactor:ma,OneMinusConstantColorFactor:ca,OneMinusDstAlphaFactor:da,OneMinusDstColorFactor:ga,OneMinusSrcAlphaFactor:Ca,OneMinusSrcColorFactor:Ta,OrthographicCamera:Q,P3Primaries:Sa,PCFShadowMap:va,PCFSoftShadowMap:Ma,PMREMGenerator:_a,Path:xa,PerspectiveCamera:ba,Plane:Ra,PlaneGeometry:Aa,PlaneHelper:Ba,PointLight:Fa,PointLightHelper:ya,Points:Pa,PointsMaterial:La,PolarGridHelper:Da,PolyhedronGeometry:Ea,PositionalAudio:Ga,PropertyBinding:wa,PropertyMixer:Ua,QuadraticBezierCurve:Na,QuadraticBezierCurve3:Ia,Quaternion:Oa,QuaternionKeyframeTrack:Ha,QuaternionLinearInterpolant:Va,RED_GREEN_RGTC2_Format:za,RED_RGTC1_Format:Qa,REVISION:ka,RGBADepthPacking:Wa,RGBAFormat:qa,RGBAIntegerFormat:Ka,RGBA_ASTC_10x10_Format:ja,RGBA_ASTC_10x5_Format:Xa,RGBA_ASTC_10x6_Format:Za,RGBA_ASTC_10x8_Format:Ya,RGBA_ASTC_12x10_Format:Ja,RGBA_ASTC_12x12_Format:$a,RGBA_ASTC_4x4_Format:es,RGBA_ASTC_5x4_Format:ts,RGBA_ASTC_5x5_Format:rs,RGBA_ASTC_6x5_Format:is,RGBA_ASTC_6x6_Format:as,RGBA_ASTC_8x5_Format:ss,RGBA_ASTC_8x6_Format:os,RGBA_ASTC_8x8_Format:ls,RGBA_BPTC_Format:ns,RGBA_ETC2_EAC_Format:us,RGBA_PVRTC_2BPPV1_Format:hs,RGBA_PVRTC_4BPPV1_Format:fs,RGBA_S3TC_DXT1_Format:ps,RGBA_S3TC_DXT3_Format:ms,RGBA_S3TC_DXT5_Format:cs,RGB_BPTC_SIGNED_Format:ds,RGB_BPTC_UNSIGNED_Format:gs,RGB_ETC1_Format:Cs,RGB_ETC2_Format:Ts,RGB_PVRTC_2BPPV1_Format:Ss,RGB_PVRTC_4BPPV1_Format:vs,RGB_S3TC_DXT1_Format:Ms,RGFormat:_s,RGIntegerFormat:xs,RawShaderMaterial:k,Ray:bs,Raycaster:Rs,Rec709Primaries:As,RectAreaLight:Bs,RedFormat:Fs,RedIntegerFormat:ys,ReinhardToneMapping:W,RenderTarget:Ps,RepeatWrapping:Ls,ReplaceStencilOp:Ds,ReverseSubtractEquation:Es,RingGeometry:Gs,SIGNED_RED_GREEN_RGTC2_Format:ws,SIGNED_RED_RGTC1_Format:Us,SRGBColorSpace:Ns,SRGBTransfer:q,Scene:Is,ShaderChunk:Os,ShaderLib:Hs,ShaderMaterial:p,ShadowMaterial:Vs,Shape:zs,ShapeGeometry:Qs,ShapePath:ks,ShapeUtils:Ws,ShortType:qs,Skeleton:Ks,SkeletonHelper:js,SkinnedMesh:Xs,Source:Zs,Sphere:Ys,SphereGeometry:Js,Spherical:$s,SphericalHarmonics3:eo,SplineCurve:to,SpotLight:ro,SpotLightHelper:io,Sprite:ao,SpriteMaterial:so,SrcAlphaFactor:oo,SrcAlphaSaturateFactor:lo,SrcColorFactor:no,StaticCopyUsage:uo,StaticDrawUsage:ho,StaticReadUsage:fo,StereoCamera:po,StreamCopyUsage:mo,StreamDrawUsage:co,StreamReadUsage:go,StringKeyframeTrack:Co,SubtractEquation:To,SubtractiveBlending:So,TOUCH:vo,TangentSpaceNormalMap:Mo,TetrahedronGeometry:_o,Texture:xo,TextureLoader:bo,TorusGeometry:Ro,TorusKnotGeometry:Ao,Triangle:Bo,TriangleFanDrawMode:Fo,TriangleStripDrawMode:yo,TrianglesDrawMode:Po,TubeGeometry:Lo,TwoPassDoubleSide:Do,UVMapping:Eo,Uint16BufferAttribute:Go,Uint32BufferAttribute:wo,Uint8BufferAttribute:Uo,Uint8ClampedBufferAttribute:No,Uniform:Io,UniformsGroup:Oo,UniformsLib:Ho,UniformsUtils:d,UnsignedByteType:Vo,UnsignedInt248Type:zo,UnsignedIntType:Qo,UnsignedShort4444Type:ko,UnsignedShort5551Type:Wo,UnsignedShortType:qo,VSMShadowMap:Ko,Vector2:u,Vector3:C,Vector4:jo,VectorKeyframeTrack:Xo,VideoTexture:Zo,WebGL1Renderer:Yo,WebGL3DRenderTarget:Jo,WebGLArrayRenderTarget:$o,WebGLCoordinateSystem:el,WebGLCubeRenderTarget:tl,WebGLMultipleRenderTargets:rl,WebGLRenderTarget:T,WebGLRenderer:il,WebGLUtils:al,WebGPUCoordinateSystem:sl,WireframeGeometry:ol,WrapAroundEnding:ll,ZeroCurvatureEnding:nl,ZeroFactor:ul,ZeroSlopeEnding:hl,ZeroStencilOp:fl,_SRGBAFormat:pl,createCanvasElement:ml,sRGBEncoding:cl}=Z;var M={name:"CopyShader",uniforms:{tDiffuse:{value:null},opacity:{value:1}},vertexShader:`

		varying vec2 vUv;

		void main() {

			vUv = uv;
			gl_Position = projectionMatrix * modelViewMatrix * vec4( position, 1.0 );

		}`,fragmentShader:`

		uniform float opacity;

		uniform sampler2D tDiffuse;

		varying vec2 vUv;

		void main() {

			vec4 texel = texture2D( tDiffuse, vUv );
			gl_FragColor = opacity * texel;


		}`};var n=class{constructor(){this.isPass=!0,this.enabled=!0,this.needsSwap=!0,this.clear=!1,this.renderToScreen=!1}setSize(){}render(){console.error("THREE.Pass: .render() must be implemented in derived pass.")}dispose(){}},Y=new Q(-1,1,1,-1,0,1),L=class extends w{constructor(){super(),this.setAttribute("position",new P([-1,3,0,-1,-1,0,3,-1,0],3)),this.setAttribute("uv",new P([0,2,0,0,2,0],2))}},J=new L,m=class{constructor(e){this._mesh=new H(J,e)}dispose(){this._mesh.geometry.dispose()}render(e){e.render(this._mesh,Y)}get material(){return this._mesh.material}set material(e){this._mesh.material=e}};var _=class extends n{constructor(e,t){super(),this.textureID=t!==void 0?t:"tDiffuse",e instanceof p?(this.uniforms=e.uniforms,this.material=e):e&&(this.uniforms=d.clone(e.uniforms),this.material=new p({name:e.name!==void 0?e.name:"unspecified",defines:Object.assign({},e.defines),uniforms:this.uniforms,vertexShader:e.vertexShader,fragmentShader:e.fragmentShader})),this.fsQuad=new m(this.material)}render(e,t,i){this.uniforms[this.textureID]&&(this.uniforms[this.textureID].value=i.texture),this.fsQuad.material=this.material,this.renderToScreen?(e.setRenderTarget(null),this.fsQuad.render(e)):(e.setRenderTarget(t),this.clear&&e.clear(e.autoClearColor,e.autoClearDepth,e.autoClearStencil),this.fsQuad.render(e))}dispose(){this.material.dispose(),this.fsQuad.dispose()}};var v=class extends n{constructor(e,t){super(),this.scene=e,this.camera=t,this.clear=!0,this.needsSwap=!1,this.inverse=!1}render(e,t,i){let a=e.getContext(),r=e.state;r.buffers.color.setMask(!1),r.buffers.depth.setMask(!1),r.buffers.color.setLocked(!0),r.buffers.depth.setLocked(!0);let s,l;this.inverse?(s=0,l=1):(s=1,l=0),r.buffers.stencil.setTest(!0),r.buffers.stencil.setOp(a.REPLACE,a.REPLACE,a.REPLACE),r.buffers.stencil.setFunc(a.ALWAYS,s,4294967295),r.buffers.stencil.setClear(l),r.buffers.stencil.setLocked(!0),e.setRenderTarget(i),this.clear&&e.clear(),e.render(this.scene,this.camera),e.setRenderTarget(t),this.clear&&e.clear(),e.render(this.scene,this.camera),r.buffers.color.setLocked(!1),r.buffers.depth.setLocked(!1),r.buffers.color.setMask(!0),r.buffers.depth.setMask(!0),r.buffers.stencil.setLocked(!1),r.buffers.stencil.setFunc(a.EQUAL,1,4294967295),r.buffers.stencil.setOp(a.KEEP,a.KEEP,a.KEEP),r.buffers.stencil.setLocked(!0)}},x=class extends n{constructor(){super(),this.needsSwap=!1}render(e){e.state.buffers.stencil.setLocked(!1),e.state.buffers.stencil.setTest(!1)}};var b=class{constructor(e,t){if(this.renderer=e,this._pixelRatio=e.getPixelRatio(),t===void 0){let i=e.getSize(new u);this._width=i.width,this._height=i.height,t=new T(this._width*this._pixelRatio,this._height*this._pixelRatio,{type:g}),t.texture.name="EffectComposer.rt1"}else this._width=t.width,this._height=t.height;this.renderTarget1=t,this.renderTarget2=t.clone(),this.renderTarget2.texture.name="EffectComposer.rt2",this.writeBuffer=this.renderTarget1,this.readBuffer=this.renderTarget2,this.renderToScreen=!0,this.passes=[],this.copyPass=new _(M),this.copyPass.material.blending=z,this.clock=new N}swapBuffers(){let e=this.readBuffer;this.readBuffer=this.writeBuffer,this.writeBuffer=e}addPass(e){this.passes.push(e),e.setSize(this._width*this._pixelRatio,this._height*this._pixelRatio)}insertPass(e,t){this.passes.splice(t,0,e),e.setSize(this._width*this._pixelRatio,this._height*this._pixelRatio)}removePass(e){let t=this.passes.indexOf(e);t!==-1&&this.passes.splice(t,1)}isLastEnabledPass(e){for(let t=e+1;t<this.passes.length;t++)if(this.passes[t].enabled)return!1;return!0}render(e){e===void 0&&(e=this.clock.getDelta());let t=this.renderer.getRenderTarget(),i=!1;for(let a=0,r=this.passes.length;a<r;a++){let s=this.passes[a];if(s.enabled!==!1){if(s.renderToScreen=this.renderToScreen&&this.isLastEnabledPass(a),s.render(this.renderer,this.writeBuffer,this.readBuffer,e,i),s.needsSwap){if(i){let l=this.renderer.getContext(),o=this.renderer.state.buffers.stencil;o.setFunc(l.NOTEQUAL,1,4294967295),this.copyPass.render(this.renderer,this.writeBuffer,this.readBuffer,e),o.setFunc(l.EQUAL,1,4294967295)}this.swapBuffers()}v!==void 0&&(s instanceof v?i=!0:s instanceof x&&(i=!1))}}this.renderer.setRenderTarget(t)}reset(e){if(e===void 0){let t=this.renderer.getSize(new u);this._pixelRatio=this.renderer.getPixelRatio(),this._width=t.width,this._height=t.height,e=this.renderTarget1.clone(),e.setSize(this._width*this._pixelRatio,this._height*this._pixelRatio)}this.renderTarget1.dispose(),this.renderTarget2.dispose(),this.renderTarget1=e,this.renderTarget2=e.clone(),this.writeBuffer=this.renderTarget1,this.readBuffer=this.renderTarget2}setSize(e,t){this._width=e,this._height=t;let i=this._width*this._pixelRatio,a=this._height*this._pixelRatio;this.renderTarget1.setSize(i,a),this.renderTarget2.setSize(i,a);for(let r=0;r<this.passes.length;r++)this.passes[r].setSize(i,a)}setPixelRatio(e){this._pixelRatio=e,this.setSize(this._width,this._height)}dispose(){this.renderTarget1.dispose(),this.renderTarget2.dispose(),this.copyPass.dispose()}};var R=class extends n{constructor(e,t,i=null,a=null,r=null){super(),this.scene=e,this.camera=t,this.overrideMaterial=i,this.clearColor=a,this.clearAlpha=r,this.clear=!0,this.clearDepth=!1,this.needsSwap=!1,this._oldClearColor=new c}render(e,t,i){let a=e.autoClear;e.autoClear=!1;let r,s;this.overrideMaterial!==null&&(s=this.scene.overrideMaterial,this.scene.overrideMaterial=this.overrideMaterial),this.clearColor!==null&&(e.getClearColor(this._oldClearColor),e.setClearColor(this.clearColor)),this.clearAlpha!==null&&(r=e.getClearAlpha(),e.setClearAlpha(this.clearAlpha)),this.clearDepth==!0&&e.clearDepth(),e.setRenderTarget(this.renderToScreen?null:i),this.clear===!0&&e.clear(e.autoClearColor,e.autoClearDepth,e.autoClearStencil),e.render(this.scene,this.camera),this.clearColor!==null&&e.setClearColor(this._oldClearColor),this.clearAlpha!==null&&e.setClearAlpha(r),this.overrideMaterial!==null&&(this.scene.overrideMaterial=s),e.autoClear=a}};var K={name:"LuminosityHighPassShader",shaderID:"luminosityHighPass",uniforms:{tDiffuse:{value:null},luminosityThreshold:{value:1},smoothWidth:{value:1},defaultColor:{value:new c(0)},defaultOpacity:{value:0}},vertexShader:`

		varying vec2 vUv;

		void main() {

			vUv = uv;

			gl_Position = projectionMatrix * modelViewMatrix * vec4( position, 1.0 );

		}`,fragmentShader:`

		uniform sampler2D tDiffuse;
		uniform vec3 defaultColor;
		uniform float defaultOpacity;
		uniform float luminosityThreshold;
		uniform float smoothWidth;

		varying vec2 vUv;

		void main() {

			vec4 texel = texture2D( tDiffuse, vUv );

			vec3 luma = vec3( 0.299, 0.587, 0.114 );

			float v = dot( texel.xyz, luma );

			vec4 outputColor = vec4( defaultColor.rgb, defaultOpacity );

			float alpha = smoothstep( luminosityThreshold, luminosityThreshold + smoothWidth, v );

			gl_FragColor = mix( outputColor, texel, alpha );

		}`};var S=class h extends n{constructor(e,t,i,a){super(),this.strength=t!==void 0?t:1,this.radius=i,this.threshold=a,this.resolution=e!==void 0?new u(e.x,e.y):new u(256,256),this.clearColor=new c(0,0,0),this.renderTargetsHorizontal=[],this.renderTargetsVertical=[],this.nMips=5;let r=Math.round(this.resolution.x/2),s=Math.round(this.resolution.y/2);this.renderTargetBright=new T(r,s,{type:g}),this.renderTargetBright.texture.name="UnrealBloomPass.bright",this.renderTargetBright.texture.generateMipmaps=!1;for(let f=0;f<this.nMips;f++){let F=new T(r,s,{type:g});F.texture.name="UnrealBloomPass.h"+f,F.texture.generateMipmaps=!1,this.renderTargetsHorizontal.push(F);let y=new T(r,s,{type:g});y.texture.name="UnrealBloomPass.v"+f,y.texture.generateMipmaps=!1,this.renderTargetsVertical.push(y),r=Math.round(r/2),s=Math.round(s/2)}let l=K;this.highPassUniforms=d.clone(l.uniforms),this.highPassUniforms.luminosityThreshold.value=a,this.highPassUniforms.smoothWidth.value=.01,this.materialHighPassFilter=new p({uniforms:this.highPassUniforms,vertexShader:l.vertexShader,fragmentShader:l.fragmentShader}),this.separableBlurMaterials=[];let o=[3,5,7,9,11];r=Math.round(this.resolution.x/2),s=Math.round(this.resolution.y/2);for(let f=0;f<this.nMips;f++)this.separableBlurMaterials.push(this.getSeperableBlurMaterial(o[f])),this.separableBlurMaterials[f].uniforms.invSize.value=new u(1/r,1/s),r=Math.round(r/2),s=Math.round(s/2);this.compositeMaterial=this.getCompositeMaterial(this.nMips),this.compositeMaterial.uniforms.blurTexture1.value=this.renderTargetsVertical[0].texture,this.compositeMaterial.uniforms.blurTexture2.value=this.renderTargetsVertical[1].texture,this.compositeMaterial.uniforms.blurTexture3.value=this.renderTargetsVertical[2].texture,this.compositeMaterial.uniforms.blurTexture4.value=this.renderTargetsVertical[3].texture,this.compositeMaterial.uniforms.blurTexture5.value=this.renderTargetsVertical[4].texture,this.compositeMaterial.uniforms.bloomStrength.value=t,this.compositeMaterial.uniforms.bloomRadius.value=.1;let X=[1,.8,.6,.4,.2];this.compositeMaterial.uniforms.bloomFactors.value=X,this.bloomTintColors=[new C(1,1,1),new C(1,1,1),new C(1,1,1),new C(1,1,1),new C(1,1,1)],this.compositeMaterial.uniforms.bloomTintColors.value=this.bloomTintColors;let B=M;this.copyUniforms=d.clone(B.uniforms),this.blendMaterial=new p({uniforms:this.copyUniforms,vertexShader:B.vertexShader,fragmentShader:B.fragmentShader,blending:E,depthTest:!1,depthWrite:!1,transparent:!0}),this.enabled=!0,this.needsSwap=!1,this._oldClearColor=new c,this.oldClearAlpha=1,this.basic=new V,this.fsQuad=new m(null)}dispose(){for(let e=0;e<this.renderTargetsHorizontal.length;e++)this.renderTargetsHorizontal[e].dispose();for(let e=0;e<this.renderTargetsVertical.length;e++)this.renderTargetsVertical[e].dispose();this.renderTargetBright.dispose();for(let e=0;e<this.separableBlurMaterials.length;e++)this.separableBlurMaterials[e].dispose();this.compositeMaterial.dispose(),this.blendMaterial.dispose(),this.basic.dispose(),this.fsQuad.dispose()}setSize(e,t){let i=Math.round(e/2),a=Math.round(t/2);this.renderTargetBright.setSize(i,a);for(let r=0;r<this.nMips;r++)this.renderTargetsHorizontal[r].setSize(i,a),this.renderTargetsVertical[r].setSize(i,a),this.separableBlurMaterials[r].uniforms.invSize.value=new u(1/i,1/a),i=Math.round(i/2),a=Math.round(a/2)}render(e,t,i,a,r){e.getClearColor(this._oldClearColor),this.oldClearAlpha=e.getClearAlpha();let s=e.autoClear;e.autoClear=!1,e.setClearColor(this.clearColor,0),r&&e.state.buffers.stencil.setTest(!1),this.renderToScreen&&(this.fsQuad.material=this.basic,this.basic.map=i.texture,e.setRenderTarget(null),e.clear(),this.fsQuad.render(e)),this.highPassUniforms.tDiffuse.value=i.texture,this.highPassUniforms.luminosityThreshold.value=this.threshold,this.fsQuad.material=this.materialHighPassFilter,e.setRenderTarget(this.renderTargetBright),e.clear(),this.fsQuad.render(e);let l=this.renderTargetBright;for(let o=0;o<this.nMips;o++)this.fsQuad.material=this.separableBlurMaterials[o],this.separableBlurMaterials[o].uniforms.colorTexture.value=l.texture,this.separableBlurMaterials[o].uniforms.direction.value=h.BlurDirectionX,e.setRenderTarget(this.renderTargetsHorizontal[o]),e.clear(),this.fsQuad.render(e),this.separableBlurMaterials[o].uniforms.colorTexture.value=this.renderTargetsHorizontal[o].texture,this.separableBlurMaterials[o].uniforms.direction.value=h.BlurDirectionY,e.setRenderTarget(this.renderTargetsVertical[o]),e.clear(),this.fsQuad.render(e),l=this.renderTargetsVertical[o];this.fsQuad.material=this.compositeMaterial,this.compositeMaterial.uniforms.bloomStrength.value=this.strength,this.compositeMaterial.uniforms.bloomRadius.value=this.radius,this.compositeMaterial.uniforms.bloomTintColors.value=this.bloomTintColors,e.setRenderTarget(this.renderTargetsHorizontal[0]),e.clear(),this.fsQuad.render(e),this.fsQuad.material=this.blendMaterial,this.copyUniforms.tDiffuse.value=this.renderTargetsHorizontal[0].texture,r&&e.state.buffers.stencil.setTest(!0),this.renderToScreen?(e.setRenderTarget(null),this.fsQuad.render(e)):(e.setRenderTarget(i),this.fsQuad.render(e)),e.setClearColor(this._oldClearColor,this.oldClearAlpha),e.autoClear=s}getSeperableBlurMaterial(e){let t=[];for(let i=0;i<e;i++)t.push(.39894*Math.exp(-.5*i*i/(e*e))/e);return new p({defines:{KERNEL_RADIUS:e},uniforms:{colorTexture:{value:null},invSize:{value:new u(.5,.5)},direction:{value:new u(.5,.5)},gaussianCoefficients:{value:t}},vertexShader:`varying vec2 vUv;
				void main() {
					vUv = uv;
					gl_Position = projectionMatrix * modelViewMatrix * vec4( position, 1.0 );
				}`,fragmentShader:`#include <common>
				varying vec2 vUv;
				uniform sampler2D colorTexture;
				uniform vec2 invSize;
				uniform vec2 direction;
				uniform float gaussianCoefficients[KERNEL_RADIUS];

				void main() {
					float weightSum = gaussianCoefficients[0];
					vec3 diffuseSum = texture2D( colorTexture, vUv ).rgb * weightSum;
					for( int i = 1; i < KERNEL_RADIUS; i ++ ) {
						float x = float(i);
						float w = gaussianCoefficients[i];
						vec2 uvOffset = direction * invSize * x;
						vec3 sample1 = texture2D( colorTexture, vUv + uvOffset ).rgb;
						vec3 sample2 = texture2D( colorTexture, vUv - uvOffset ).rgb;
						diffuseSum += (sample1 + sample2) * w;
						weightSum += 2.0 * w;
					}
					gl_FragColor = vec4(diffuseSum/weightSum, 1.0);
				}`})}getCompositeMaterial(e){return new p({defines:{NUM_MIPS:e},uniforms:{blurTexture1:{value:null},blurTexture2:{value:null},blurTexture3:{value:null},blurTexture4:{value:null},blurTexture5:{value:null},bloomStrength:{value:1},bloomFactors:{value:null},bloomTintColors:{value:null},bloomRadius:{value:0}},vertexShader:`varying vec2 vUv;
				void main() {
					vUv = uv;
					gl_Position = projectionMatrix * modelViewMatrix * vec4( position, 1.0 );
				}`,fragmentShader:`varying vec2 vUv;
				uniform sampler2D blurTexture1;
				uniform sampler2D blurTexture2;
				uniform sampler2D blurTexture3;
				uniform sampler2D blurTexture4;
				uniform sampler2D blurTexture5;
				uniform float bloomStrength;
				uniform float bloomRadius;
				uniform float bloomFactors[NUM_MIPS];
				uniform vec3 bloomTintColors[NUM_MIPS];

				float lerpBloomFactor(const in float factor) {
					float mirrorFactor = 1.2 - factor;
					return mix(factor, mirrorFactor, bloomRadius);
				}

				void main() {
					gl_FragColor = bloomStrength * ( lerpBloomFactor(bloomFactors[0]) * vec4(bloomTintColors[0], 1.0) * texture2D(blurTexture1, vUv) +
						lerpBloomFactor(bloomFactors[1]) * vec4(bloomTintColors[1], 1.0) * texture2D(blurTexture2, vUv) +
						lerpBloomFactor(bloomFactors[2]) * vec4(bloomTintColors[2], 1.0) * texture2D(blurTexture3, vUv) +
						lerpBloomFactor(bloomFactors[3]) * vec4(bloomTintColors[3], 1.0) * texture2D(blurTexture4, vUv) +
						lerpBloomFactor(bloomFactors[4]) * vec4(bloomTintColors[4], 1.0) * texture2D(blurTexture5, vUv) );
				}`})}};S.BlurDirectionX=new u(1,0);S.BlurDirectionY=new u(0,1);var j={name:"OutputShader",uniforms:{tDiffuse:{value:null},toneMappingExposure:{value:1}},vertexShader:`
		precision highp float;

		uniform mat4 modelViewMatrix;
		uniform mat4 projectionMatrix;

		attribute vec3 position;
		attribute vec2 uv;

		varying vec2 vUv;

		void main() {

			vUv = uv;
			gl_Position = projectionMatrix * modelViewMatrix * vec4( position, 1.0 );

		}`,fragmentShader:`
	
		precision highp float;

		uniform sampler2D tDiffuse;

		#include <tonemapping_pars_fragment>
		#include <colorspace_pars_fragment>

		varying vec2 vUv;

		void main() {

			gl_FragColor = texture2D( tDiffuse, vUv );

			// tone mapping

			#ifdef LINEAR_TONE_MAPPING

				gl_FragColor.rgb = LinearToneMapping( gl_FragColor.rgb );

			#elif defined( REINHARD_TONE_MAPPING )

				gl_FragColor.rgb = ReinhardToneMapping( gl_FragColor.rgb );

			#elif defined( CINEON_TONE_MAPPING )

				gl_FragColor.rgb = OptimizedCineonToneMapping( gl_FragColor.rgb );

			#elif defined( ACES_FILMIC_TONE_MAPPING )

				gl_FragColor.rgb = ACESFilmicToneMapping( gl_FragColor.rgb );

			#elif defined( AGX_TONE_MAPPING )

				gl_FragColor.rgb = AgXToneMapping( gl_FragColor.rgb );

			#endif

			// color space

			#ifdef SRGB_TRANSFER

				gl_FragColor = sRGBTransferOETF( gl_FragColor );

			#endif

		}`};var A=class extends n{constructor(){super();let e=j;this.uniforms=d.clone(e.uniforms),this.material=new k({name:e.name,uniforms:this.uniforms,vertexShader:e.vertexShader,fragmentShader:e.fragmentShader}),this.fsQuad=new m(this.material),this._outputColorSpace=null,this._toneMapping=null}render(e,t,i){this.uniforms.tDiffuse.value=i.texture,this.uniforms.toneMappingExposure.value=e.toneMappingExposure,(this._outputColorSpace!==e.outputColorSpace||this._toneMapping!==e.toneMapping)&&(this._outputColorSpace=e.outputColorSpace,this._toneMapping=e.toneMapping,this.material.defines={},I.getTransfer(this._outputColorSpace)===q&&(this.material.defines.SRGB_TRANSFER=""),this._toneMapping===O?this.material.defines.LINEAR_TONE_MAPPING="":this._toneMapping===W?this.material.defines.REINHARD_TONE_MAPPING="":this._toneMapping===U?this.material.defines.CINEON_TONE_MAPPING="":this._toneMapping===D?this.material.defines.ACES_FILMIC_TONE_MAPPING="":this._toneMapping===G&&(this.material.defines.AGX_TONE_MAPPING=""),this.material.needsUpdate=!0),this.renderToScreen===!0?(e.setRenderTarget(null),this.fsQuad.render(e)):(e.setRenderTarget(t),this.clear&&e.clear(e.autoClearColor,e.autoClearDepth,e.autoClearStencil),this.fsQuad.render(e))}dispose(){this.material.dispose(),this.fsQuad.dispose()}};window.THREE.EffectComposer=b;window.THREE.RenderPass=R;window.THREE.UnrealBloomPass=S;window.THREE.OutputPass=A;})();
