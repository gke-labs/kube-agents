// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package fakeapiserver

import (
	"fmt"
	"mime"
	"net/http"
	"strings"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/runtime/serializer/streaming"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/kubernetes/scheme"
)

const (
	acceptHeader      = "Accept"
	contentTypeHeader = "Content-Type"
	acceptAny         = "*/*"
	acceptSeparator   = ","
	// watchStreamSuffix is what the API server appends to the media type of
	// a watch response in any format but JSON; client-go's stream decoder
	// is picked from the media type with the parameter stripped.
	watchStreamSuffix = ";stream=watch"
)

// wireFormat is one of the two encodings the fake serves, picked per request
// from the Accept header: JSON, or the Kubernetes protobuf that client-go asks
// for first on every typed client. Both come from client-go's own codec
// factory, so what the fake writes is what the real API server writes for the
// same media type, list bodies, watch frames and Status errors alike.
type wireFormat struct {
	info runtime.SerializerInfo
}

var (
	jsonFormat     = mustFormat(runtime.ContentTypeJSON)
	protobufFormat = mustFormat(runtime.ContentTypeProtobuf)
)

func mustFormat(mediaType string) wireFormat {
	info, ok := runtime.SerializerInfoForMediaType(scheme.Codecs.SupportedMediaTypes(), mediaType)
	if !ok {
		panic(fmt.Sprintf("fakeapiserver: client-go's scheme has no serializer for %s", mediaType))
	}
	return wireFormat{info: info}
}

// negotiate reads an Accept header the way the API server does for the two
// formats it serves: the first entry naming protobuf or JSON wins, `*/*` and
// an empty header mean JSON. A client-go Event client sends
// `application/vnd.kubernetes.protobuf,application/json` and gets protobuf; a
// config that names JSON gets JSON.
func negotiate(accept string) wireFormat {
	for _, part := range strings.Split(accept, acceptSeparator) {
		mediaType, _, err := mime.ParseMediaType(strings.TrimSpace(part))
		if err != nil {
			continue
		}
		switch mediaType {
		case runtime.ContentTypeProtobuf:
			return protobufFormat
		case runtime.ContentTypeJSON, acceptAny:
			return jsonFormat
		}
	}
	return jsonFormat
}

// MediaType is the format's Content-Type for a list or a single object.
func (f wireFormat) MediaType() string { return f.info.MediaType }

// encoder encodes one object as a member of gv.
func (f wireFormat) encoder(gv schema.GroupVersion) runtime.Encoder {
	return scheme.Codecs.EncoderForVersion(f.info.Serializer, gv)
}

// writeObject writes obj as the whole response: the format's Content-Type,
// the status code, and the body.
func (f wireFormat) writeObject(w http.ResponseWriter, code int, gv schema.GroupVersion, obj runtime.Object) error {
	w.Header().Set(contentTypeHeader, f.info.MediaType)
	w.WriteHeader(code)
	return f.encoder(gv).Encode(obj, w)
}

// writeStatus writes a metav1.Status as the response, with the status's own
// code. Status is unversioned in the scheme, so it is written under its own
// v1 kind whichever gv the request was for.
func (f wireFormat) writeStatus(w http.ResponseWriter, status *metav1.Status) {
	_ = f.writeObject(w, int(status.Code), metav1.SchemeGroupVersion, status)
}

// watchWriter frames watch events the way the API server does: each event is
// a metav1.WatchEvent carrying the object encoded in the negotiated format,
// framed by the format's stream serializer (newline-delimited JSON, or
// length-delimited protobuf) and flushed so a client reading the stream sees
// it before the next one is written.
type watchWriter struct {
	embedded runtime.Encoder
	stream   streaming.Encoder
	flusher  http.Flusher
}

// startWatch writes the headers of a watch response, 200 and the stream
// media type, and returns the writer for its events. The status is written
// here, so a stream that then carries only an Error event is still a 200
// with that event in it, which is how the API server reports a watch it
// cannot serve from the requested resource version.
func (f wireFormat) startWatch(w http.ResponseWriter, gv schema.GroupVersion) *watchWriter {
	mediaType := f.info.MediaType
	if mediaType != runtime.ContentTypeJSON {
		mediaType += watchStreamSuffix
	}
	w.Header().Set(contentTypeHeader, mediaType)
	w.WriteHeader(http.StatusOK)
	flusher, _ := w.(http.Flusher)
	framer := f.info.StreamSerializer.Framer.NewFrameWriter(w)
	ww := &watchWriter{
		embedded: f.encoder(gv),
		stream:   streaming.NewEncoder(framer, scheme.Codecs.EncoderForVersion(f.info.StreamSerializer.Serializer, gv)),
		flusher:  flusher,
	}
	ww.flush()
	return ww
}

// send writes one event. It does not flush; the caller flushes a batch.
func (ww *watchWriter) send(t watch.EventType, obj runtime.Object) error {
	raw, err := runtime.Encode(ww.embedded, obj)
	if err != nil {
		return err
	}
	return ww.stream.Encode(&metav1.WatchEvent{Type: string(t), Object: runtime.RawExtension{Raw: raw}})
}

func (ww *watchWriter) flush() {
	if ww.flusher != nil {
		ww.flusher.Flush()
	}
}
