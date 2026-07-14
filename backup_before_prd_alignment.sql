--
-- PostgreSQL database dump
--

\restrict 7ezXQeSDIbmf58sPhcvdGMnu6k7NLzuRt04Kg7cvwZFZ3Rf8zkMb8uzmeTjoyK6

-- Dumped from database version 16.14
-- Dumped by pg_dump version 16.14

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: annotation_status_enum; Type: TYPE; Schema: public; Owner: openformat
--

CREATE TYPE public.annotation_status_enum AS ENUM (
    'open',
    'resolved',
    'in_review'
);


ALTER TYPE public.annotation_status_enum OWNER TO openformat;

--
-- Name: file_format_enum; Type: TYPE; Schema: public; Owner: openformat
--

CREATE TYPE public.file_format_enum AS ENUM (
    'ifc',
    'gltf',
    'glb',
    'step',
    'stp',
    'obj',
    'stl'
);


ALTER TYPE public.file_format_enum OWNER TO openformat;

--
-- Name: member_role_enum; Type: TYPE; Schema: public; Owner: openformat
--

CREATE TYPE public.member_role_enum AS ENUM (
    'viewer',
    'editor',
    'admin'
);


ALTER TYPE public.member_role_enum OWNER TO openformat;

--
-- Name: model_status_enum; Type: TYPE; Schema: public; Owner: openformat
--

CREATE TYPE public.model_status_enum AS ENUM (
    'pending',
    'processing',
    'ready',
    'failed'
);


ALTER TYPE public.model_status_enum OWNER TO openformat;

--
-- Name: plan_enum; Type: TYPE; Schema: public; Owner: openformat
--

CREATE TYPE public.plan_enum AS ENUM (
    'free',
    'pro',
    'enterprise'
);


ALTER TYPE public.plan_enum OWNER TO openformat;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: openformat
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


ALTER TABLE public.alembic_version OWNER TO openformat;

--
-- Name: annotation_comments; Type: TABLE; Schema: public; Owner: openformat
--

CREATE TABLE public.annotation_comments (
    id uuid NOT NULL,
    annotation_id uuid NOT NULL,
    author_id uuid NOT NULL,
    body text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.annotation_comments OWNER TO openformat;

--
-- Name: annotations; Type: TABLE; Schema: public; Owner: openformat
--

CREATE TABLE public.annotations (
    id uuid NOT NULL,
    model_id uuid NOT NULL,
    created_by uuid NOT NULL,
    title character varying(500) NOT NULL,
    body text,
    "position" jsonb,
    status public.annotation_status_enum NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.annotations OWNER TO openformat;

--
-- Name: api_keys; Type: TABLE; Schema: public; Owner: openformat
--

CREATE TABLE public.api_keys (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    name character varying(255) NOT NULL,
    key_hash character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    revoked_at timestamp with time zone
);


ALTER TABLE public.api_keys OWNER TO openformat;

--
-- Name: model_elements; Type: TABLE; Schema: public; Owner: openformat
--

CREATE TABLE public.model_elements (
    id uuid NOT NULL,
    model_id uuid NOT NULL,
    guid character varying(255) NOT NULL,
    element_type character varying(255),
    name character varying(500),
    properties jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.model_elements OWNER TO openformat;

--
-- Name: model_metadata; Type: TABLE; Schema: public; Owner: openformat
--

CREATE TABLE public.model_metadata (
    id uuid NOT NULL,
    model_id uuid NOT NULL,
    properties jsonb,
    spatial_tree jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.model_metadata OWNER TO openformat;

--
-- Name: models; Type: TABLE; Schema: public; Owner: openformat
--

CREATE TABLE public.models (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    uploaded_by uuid NOT NULL,
    original_filename character varying(500) NOT NULL,
    file_format public.file_format_enum NOT NULL,
    s3_raw_key character varying(1000),
    s3_processed_prefix character varying(1000),
    file_size_bytes bigint,
    status public.model_status_enum NOT NULL,
    error_message text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    name character varying(500),
    element_count bigint,
    bounds_min_xyz jsonb,
    bounds_max_xyz jsonb
);


ALTER TABLE public.models OWNER TO openformat;

--
-- Name: project_members; Type: TABLE; Schema: public; Owner: openformat
--

CREATE TABLE public.project_members (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    user_id uuid NOT NULL,
    role public.member_role_enum NOT NULL,
    joined_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.project_members OWNER TO openformat;

--
-- Name: projects; Type: TABLE; Schema: public; Owner: openformat
--

CREATE TABLE public.projects (
    id uuid NOT NULL,
    owner_id uuid NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.projects OWNER TO openformat;

--
-- Name: share_links; Type: TABLE; Schema: public; Owner: openformat
--

CREATE TABLE public.share_links (
    id uuid NOT NULL,
    model_id uuid NOT NULL,
    created_by uuid NOT NULL,
    token character varying(64) NOT NULL,
    expires_at timestamp with time zone,
    revoked boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.share_links OWNER TO openformat;

--
-- Name: users; Type: TABLE; Schema: public; Owner: openformat
--

CREATE TABLE public.users (
    id uuid NOT NULL,
    email character varying(255) NOT NULL,
    password_hash character varying(255),
    full_name character varying(255),
    plan public.plan_enum NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    provider character varying(50),
    provider_user_id character varying(255)
);


ALTER TABLE public.users OWNER TO openformat;

--
-- Name: webhook_delivery_logs; Type: TABLE; Schema: public; Owner: openformat
--

CREATE TABLE public.webhook_delivery_logs (
    id uuid NOT NULL,
    webhook_id uuid NOT NULL,
    delivery_id character varying(36) NOT NULL,
    event character varying(255) NOT NULL,
    status_code integer,
    success boolean DEFAULT false NOT NULL,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.webhook_delivery_logs OWNER TO openformat;

--
-- Name: webhooks; Type: TABLE; Schema: public; Owner: openformat
--

CREATE TABLE public.webhooks (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    url character varying(2000) NOT NULL,
    secret character varying(255) NOT NULL,
    events text[],
    is_active boolean NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.webhooks OWNER TO openformat;

--
-- Data for Name: alembic_version; Type: TABLE DATA; Schema: public; Owner: openformat
--

COPY public.alembic_version (version_num) FROM stdin;
b3d8f2a6c9e1
\.


--
-- Data for Name: annotation_comments; Type: TABLE DATA; Schema: public; Owner: openformat
--

COPY public.annotation_comments (id, annotation_id, author_id, body, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: annotations; Type: TABLE DATA; Schema: public; Owner: openformat
--

COPY public.annotations (id, model_id, created_by, title, body, "position", status, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: api_keys; Type: TABLE DATA; Schema: public; Owner: openformat
--

COPY public.api_keys (id, user_id, name, key_hash, created_at, revoked_at) FROM stdin;
\.


--
-- Data for Name: model_elements; Type: TABLE DATA; Schema: public; Owner: openformat
--

COPY public.model_elements (id, model_id, guid, element_type, name, properties, created_at) FROM stdin;
\.


--
-- Data for Name: model_metadata; Type: TABLE DATA; Schema: public; Owner: openformat
--

COPY public.model_metadata (id, model_id, properties, spatial_tree, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: models; Type: TABLE DATA; Schema: public; Owner: openformat
--

COPY public.models (id, project_id, uploaded_by, original_filename, file_format, s3_raw_key, s3_processed_prefix, file_size_bytes, status, error_message, created_at, updated_at, name, element_count, bounds_min_xyz, bounds_max_xyz) FROM stdin;
\.


--
-- Data for Name: project_members; Type: TABLE DATA; Schema: public; Owner: openformat
--

COPY public.project_members (id, project_id, user_id, role, joined_at) FROM stdin;
b9ca09a8-0a2c-417c-aae4-95e48942ae15	54b85a3a-f5cf-4b62-b695-3fb60a28a472	18a4213c-c0ed-4d77-9e81-457cfc2d15b0	admin	2026-07-14 06:06:11.048463+00
\.


--
-- Data for Name: projects; Type: TABLE DATA; Schema: public; Owner: openformat
--

COPY public.projects (id, owner_id, name, description, created_at, updated_at) FROM stdin;
54b85a3a-f5cf-4b62-b695-3fb60a28a472	18a4213c-c0ed-4d77-9e81-457cfc2d15b0	City Center Tower	Main structural model for the tower project.	2026-07-14 06:06:11.048463+00	2026-07-14 06:06:11.048463+00
\.


--
-- Data for Name: share_links; Type: TABLE DATA; Schema: public; Owner: openformat
--

COPY public.share_links (id, model_id, created_by, token, expires_at, revoked, created_at) FROM stdin;
\.


--
-- Data for Name: users; Type: TABLE DATA; Schema: public; Owner: openformat
--

COPY public.users (id, email, password_hash, full_name, plan, created_at, updated_at, provider, provider_user_id) FROM stdin;
18a4213c-c0ed-4d77-9e81-457cfc2d15b0	user@example.com	$2b$12$EU954Pfr/94fVCfM2ibtq.euyzYxVac4lSYwatAXKvQYF4.89t376	Jane Smith	free	2026-07-14 06:05:52.169439+00	2026-07-14 06:05:52.169439+00	\N	\N
\.


--
-- Data for Name: webhook_delivery_logs; Type: TABLE DATA; Schema: public; Owner: openformat
--

COPY public.webhook_delivery_logs (id, webhook_id, delivery_id, event, status_code, success, error, created_at) FROM stdin;
\.


--
-- Data for Name: webhooks; Type: TABLE DATA; Schema: public; Owner: openformat
--

COPY public.webhooks (id, user_id, url, secret, events, is_active, created_at, updated_at) FROM stdin;
\.


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: annotation_comments annotation_comments_pkey; Type: CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.annotation_comments
    ADD CONSTRAINT annotation_comments_pkey PRIMARY KEY (id);


--
-- Name: annotations annotations_pkey; Type: CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.annotations
    ADD CONSTRAINT annotations_pkey PRIMARY KEY (id);


--
-- Name: api_keys api_keys_key_hash_key; Type: CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_key_hash_key UNIQUE (key_hash);


--
-- Name: api_keys api_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_pkey PRIMARY KEY (id);


--
-- Name: model_elements model_elements_pkey; Type: CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.model_elements
    ADD CONSTRAINT model_elements_pkey PRIMARY KEY (id);


--
-- Name: model_metadata model_metadata_pkey; Type: CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.model_metadata
    ADD CONSTRAINT model_metadata_pkey PRIMARY KEY (id);


--
-- Name: models models_pkey; Type: CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.models
    ADD CONSTRAINT models_pkey PRIMARY KEY (id);


--
-- Name: project_members project_members_pkey; Type: CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.project_members
    ADD CONSTRAINT project_members_pkey PRIMARY KEY (id);


--
-- Name: projects projects_pkey; Type: CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_pkey PRIMARY KEY (id);


--
-- Name: share_links share_links_pkey; Type: CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.share_links
    ADD CONSTRAINT share_links_pkey PRIMARY KEY (id);


--
-- Name: project_members uq_project_members_project_user; Type: CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.project_members
    ADD CONSTRAINT uq_project_members_project_user UNIQUE (project_id, user_id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: webhook_delivery_logs webhook_delivery_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.webhook_delivery_logs
    ADD CONSTRAINT webhook_delivery_logs_pkey PRIMARY KEY (id);


--
-- Name: webhooks webhooks_pkey; Type: CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.webhooks
    ADD CONSTRAINT webhooks_pkey PRIMARY KEY (id);


--
-- Name: ix_annotation_comments_annotation_id; Type: INDEX; Schema: public; Owner: openformat
--

CREATE INDEX ix_annotation_comments_annotation_id ON public.annotation_comments USING btree (annotation_id);


--
-- Name: ix_annotations_created_by; Type: INDEX; Schema: public; Owner: openformat
--

CREATE INDEX ix_annotations_created_by ON public.annotations USING btree (created_by);


--
-- Name: ix_annotations_model_created_at; Type: INDEX; Schema: public; Owner: openformat
--

CREATE INDEX ix_annotations_model_created_at ON public.annotations USING btree (model_id, created_at DESC);


--
-- Name: ix_annotations_model_id; Type: INDEX; Schema: public; Owner: openformat
--

CREATE INDEX ix_annotations_model_id ON public.annotations USING btree (model_id);


--
-- Name: ix_annotations_model_status; Type: INDEX; Schema: public; Owner: openformat
--

CREATE INDEX ix_annotations_model_status ON public.annotations USING btree (model_id, status);


--
-- Name: ix_api_keys_user_id; Type: INDEX; Schema: public; Owner: openformat
--

CREATE INDEX ix_api_keys_user_id ON public.api_keys USING btree (user_id);


--
-- Name: ix_model_elements_element_type; Type: INDEX; Schema: public; Owner: openformat
--

CREATE INDEX ix_model_elements_element_type ON public.model_elements USING btree (element_type);


--
-- Name: ix_model_elements_model_element_type; Type: INDEX; Schema: public; Owner: openformat
--

CREATE INDEX ix_model_elements_model_element_type ON public.model_elements USING btree (model_id, element_type);


--
-- Name: ix_model_elements_model_guid; Type: INDEX; Schema: public; Owner: openformat
--

CREATE INDEX ix_model_elements_model_guid ON public.model_elements USING btree (model_id, guid);


--
-- Name: ix_model_elements_model_id; Type: INDEX; Schema: public; Owner: openformat
--

CREATE INDEX ix_model_elements_model_id ON public.model_elements USING btree (model_id);


--
-- Name: ix_model_metadata_model_id; Type: INDEX; Schema: public; Owner: openformat
--

CREATE UNIQUE INDEX ix_model_metadata_model_id ON public.model_metadata USING btree (model_id);


--
-- Name: ix_models_project_created_at; Type: INDEX; Schema: public; Owner: openformat
--

CREATE INDEX ix_models_project_created_at ON public.models USING btree (project_id, created_at DESC);


--
-- Name: ix_models_project_id; Type: INDEX; Schema: public; Owner: openformat
--

CREATE INDEX ix_models_project_id ON public.models USING btree (project_id);


--
-- Name: ix_models_project_status; Type: INDEX; Schema: public; Owner: openformat
--

CREATE INDEX ix_models_project_status ON public.models USING btree (project_id, status);


--
-- Name: ix_models_status; Type: INDEX; Schema: public; Owner: openformat
--

CREATE INDEX ix_models_status ON public.models USING btree (status);


--
-- Name: ix_models_uploaded_by; Type: INDEX; Schema: public; Owner: openformat
--

CREATE INDEX ix_models_uploaded_by ON public.models USING btree (uploaded_by);


--
-- Name: ix_project_members_project_id; Type: INDEX; Schema: public; Owner: openformat
--

CREATE INDEX ix_project_members_project_id ON public.project_members USING btree (project_id);


--
-- Name: ix_project_members_user_id; Type: INDEX; Schema: public; Owner: openformat
--

CREATE INDEX ix_project_members_user_id ON public.project_members USING btree (user_id);


--
-- Name: ix_projects_owner_id; Type: INDEX; Schema: public; Owner: openformat
--

CREATE INDEX ix_projects_owner_id ON public.projects USING btree (owner_id);


--
-- Name: ix_share_links_model_id; Type: INDEX; Schema: public; Owner: openformat
--

CREATE INDEX ix_share_links_model_id ON public.share_links USING btree (model_id);


--
-- Name: ix_share_links_token; Type: INDEX; Schema: public; Owner: openformat
--

CREATE UNIQUE INDEX ix_share_links_token ON public.share_links USING btree (token);


--
-- Name: ix_users_email; Type: INDEX; Schema: public; Owner: openformat
--

CREATE UNIQUE INDEX ix_users_email ON public.users USING btree (email);


--
-- Name: ix_webhook_delivery_logs_created_at; Type: INDEX; Schema: public; Owner: openformat
--

CREATE INDEX ix_webhook_delivery_logs_created_at ON public.webhook_delivery_logs USING btree (created_at);


--
-- Name: ix_webhook_delivery_logs_delivery_id; Type: INDEX; Schema: public; Owner: openformat
--

CREATE INDEX ix_webhook_delivery_logs_delivery_id ON public.webhook_delivery_logs USING btree (delivery_id);


--
-- Name: ix_webhook_delivery_logs_webhook_id; Type: INDEX; Schema: public; Owner: openformat
--

CREATE INDEX ix_webhook_delivery_logs_webhook_id ON public.webhook_delivery_logs USING btree (webhook_id);


--
-- Name: ix_webhooks_user_id; Type: INDEX; Schema: public; Owner: openformat
--

CREATE INDEX ix_webhooks_user_id ON public.webhooks USING btree (user_id);


--
-- Name: annotation_comments annotation_comments_annotation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.annotation_comments
    ADD CONSTRAINT annotation_comments_annotation_id_fkey FOREIGN KEY (annotation_id) REFERENCES public.annotations(id) ON DELETE CASCADE;


--
-- Name: annotation_comments annotation_comments_author_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.annotation_comments
    ADD CONSTRAINT annotation_comments_author_id_fkey FOREIGN KEY (author_id) REFERENCES public.users(id);


--
-- Name: annotations annotations_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.annotations
    ADD CONSTRAINT annotations_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id);


--
-- Name: annotations annotations_model_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.annotations
    ADD CONSTRAINT annotations_model_id_fkey FOREIGN KEY (model_id) REFERENCES public.models(id) ON DELETE CASCADE;


--
-- Name: api_keys api_keys_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: model_elements model_elements_model_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.model_elements
    ADD CONSTRAINT model_elements_model_id_fkey FOREIGN KEY (model_id) REFERENCES public.models(id) ON DELETE CASCADE;


--
-- Name: model_metadata model_metadata_model_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.model_metadata
    ADD CONSTRAINT model_metadata_model_id_fkey FOREIGN KEY (model_id) REFERENCES public.models(id) ON DELETE CASCADE;


--
-- Name: models models_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.models
    ADD CONSTRAINT models_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: models models_uploaded_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.models
    ADD CONSTRAINT models_uploaded_by_fkey FOREIGN KEY (uploaded_by) REFERENCES public.users(id);


--
-- Name: project_members project_members_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.project_members
    ADD CONSTRAINT project_members_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: project_members project_members_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.project_members
    ADD CONSTRAINT project_members_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: projects projects_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: share_links share_links_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.share_links
    ADD CONSTRAINT share_links_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id);


--
-- Name: share_links share_links_model_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.share_links
    ADD CONSTRAINT share_links_model_id_fkey FOREIGN KEY (model_id) REFERENCES public.models(id) ON DELETE CASCADE;


--
-- Name: webhook_delivery_logs webhook_delivery_logs_webhook_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.webhook_delivery_logs
    ADD CONSTRAINT webhook_delivery_logs_webhook_id_fkey FOREIGN KEY (webhook_id) REFERENCES public.webhooks(id) ON DELETE CASCADE;


--
-- Name: webhooks webhooks_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: openformat
--

ALTER TABLE ONLY public.webhooks
    ADD CONSTRAINT webhooks_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict 7ezXQeSDIbmf58sPhcvdGMnu6k7NLzuRt04Kg7cvwZFZ3Rf8zkMb8uzmeTjoyK6

