
/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!50503 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `collection_posts` (
  `search_id` bigint NOT NULL,
  `post_id` bigint NOT NULL,
  `added_key` bigint NOT NULL AUTO_INCREMENT,
  `discovered_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`search_id`,`post_id`),
  UNIQUE KEY `matches_added_key` (`added_key`),
  KEY `matches_post` (`post_id`,`search_id`),
  CONSTRAINT `collection_posts_ibfk_1` FOREIGN KEY (`search_id`) REFERENCES `collection_sources` (`id`),
  CONSTRAINT `collection_posts_ibfk_2` FOREIGN KEY (`post_id`) REFERENCES `posts` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `collection_sources` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `tags` text COLLATE utf8mb4_0900_bin NOT NULL,
  `cursor` text COLLATE utf8mb4_0900_bin,
  `complete` tinyint(1) NOT NULL DEFAULT '0',
  `created` datetime(6) NOT NULL,
  `deleted_at` datetime(6) DEFAULT NULL,
  `source_id` bigint NOT NULL,
  `rating` varchar(32) COLLATE utf8mb4_0900_bin NOT NULL DEFAULT '',
  `page_offset` bigint NOT NULL DEFAULT '0',
  `page_signature` char(64) COLLATE utf8mb4_0900_bin NOT NULL DEFAULT '',
  `collection_id` bigint NOT NULL,
  `active_collection` bigint DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `active_collection_site` (`active_collection`,`source_id`),
  KEY `searches_collection_site` (`collection_id`,`source_id`),
  KEY `source_id` (`source_id`),
  CONSTRAINT `collection_sources_ibfk_1` FOREIGN KEY (`source_id`) REFERENCES `sources` (`id`),
  CONSTRAINT `collection_sources_ibfk_2` FOREIGN KEY (`collection_id`) REFERENCES `collections` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `collections` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `tags` text COLLATE utf8mb4_0900_bin NOT NULL,
  `rating` varchar(32) COLLATE utf8mb4_0900_bin NOT NULL DEFAULT '',
  `created` datetime(6) NOT NULL,
  `folder_name` varchar(191) COLLATE utf8mb4_0900_bin NOT NULL,
  `deleted_at` datetime(6) DEFAULT NULL,
  `query_key` varchar(512) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  `active_query` varchar(512) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `folder_name` (`folder_name`),
  UNIQUE KEY `active_collection_query` (`active_query`,`rating`),
  CONSTRAINT `collections_ibfk_1` FOREIGN KEY (`folder_name`) REFERENCES `library_folders` (`folder_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `crawl_jobs` (
  `job_id` bigint NOT NULL,
  `batches` int NOT NULL,
  PRIMARY KEY (`job_id`),
  CONSTRAINT `crawl_jobs_ibfk_1` FOREIGN KEY (`job_id`) REFERENCES `jobs` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `download_jobs` (
  `job_id` bigint NOT NULL,
  `media_id` bigint NOT NULL,
  `storage_root_id` bigint NOT NULL,
  `destination_folder` text COLLATE utf8mb4_0900_bin NOT NULL,
  `collection_name` varchar(191) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  `source_subdir` varchar(512) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  `requested_filename` text COLLATE utf8mb4_0900_bin,
  PRIMARY KEY (`job_id`),
  KEY `media_id` (`media_id`),
  KEY `storage_root_id` (`storage_root_id`),
  CONSTRAINT `download_jobs_ibfk_1` FOREIGN KEY (`job_id`) REFERENCES `jobs` (`id`) ON DELETE CASCADE,
  CONSTRAINT `download_jobs_ibfk_2` FOREIGN KEY (`media_id`) REFERENCES `post_media` (`id`),
  CONSTRAINT `download_jobs_ibfk_3` FOREIGN KEY (`storage_root_id`) REFERENCES `storage_roots` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `file_locations` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `file_id` bigint NOT NULL,
  `storage_root_id` bigint NOT NULL,
  `relative_path` text COLLATE utf8mb4_0900_bin NOT NULL,
  `path_hash` binary(32) NOT NULL,
  `status` enum('present','missing') COLLATE utf8mb4_0900_bin NOT NULL DEFAULT 'present',
  `downloaded_at` datetime(6) NOT NULL,
  `last_checked_at` datetime(6) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `location_path` (`storage_root_id`,`path_hash`),
  KEY `location_file` (`file_id`),
  CONSTRAINT `file_locations_ibfk_1` FOREIGN KEY (`file_id`) REFERENCES `files` (`id`),
  CONSTRAINT `file_locations_ibfk_2` FOREIGN KEY (`storage_root_id`) REFERENCES `storage_roots` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `files` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `sha256` char(64) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  `md5` varchar(128) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  `size_bytes` bigint DEFAULT NULL,
  `mime_type` varchar(128) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  `verified_at` datetime(6) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `sha256` (`sha256`),
  KEY `files_md5` (`md5`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `index_progress` (
  `name` varchar(100) COLLATE utf8mb4_0900_bin NOT NULL,
  `ready` tinyint(1) NOT NULL DEFAULT '0',
  `completed` bigint NOT NULL DEFAULT '0',
  `total` bigint NOT NULL DEFAULT '0',
  `cursor` bigint NOT NULL DEFAULT '0',
  `target` bigint DEFAULT NULL,
  `updated` datetime(6) DEFAULT NULL,
  `error` text COLLATE utf8mb4_0900_bin,
  PRIMARY KEY (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `job_attempts` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `job_id` bigint NOT NULL,
  `attempt_number` int NOT NULL,
  `started_at` datetime(6) NOT NULL,
  `finished_at` datetime(6) DEFAULT NULL,
  `outcome` varchar(32) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  `http_status` int DEFAULT NULL,
  `error_message` text COLLATE utf8mb4_0900_bin,
  PRIMARY KEY (`id`),
  UNIQUE KEY `job_attempt` (`job_id`,`attempt_number`),
  CONSTRAINT `job_attempts_ibfk_1` FOREIGN KEY (`job_id`) REFERENCES `jobs` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `jobs` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `kind` enum('crawl','download') COLLATE utf8mb4_0900_bin NOT NULL,
  `search_id` bigint NOT NULL,
  `status` varchar(32) COLLATE utf8mb4_0900_bin NOT NULL DEFAULT 'queued',
  `progress` double NOT NULL DEFAULT '0',
  `total` bigint NOT NULL,
  `message` text COLLATE utf8mb4_0900_bin NOT NULL,
  `cancel` tinyint(1) NOT NULL DEFAULT '0',
  `created` datetime(6) NOT NULL,
  `priority` int NOT NULL DEFAULT '0',
  `attempt_count` int NOT NULL DEFAULT '0',
  `max_attempts` int NOT NULL DEFAULT '3',
  `available_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `started_at` datetime(6) DEFAULT NULL,
  `finished_at` datetime(6) DEFAULT NULL,
  `download_post_id` bigint DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `jobs_status` (`status`,`id`),
  KEY `jobs_search_kind` (`search_id`,`kind`,`id` DESC),
  KEY `jobs_claim` (`kind`,`status`,`available_at`,`priority`,`id`),
  KEY `jobs_search_queue` (`search_id`,`kind`,`status`,`cancel`,`id`),
  KEY `jobs_active_download` (`search_id`,`kind`,`status`,`download_post_id`),
  KEY `jobs_same_download` (`kind`,`status`,`download_post_id`),
  KEY `download_post_id` (`download_post_id`),
  CONSTRAINT `jobs_ibfk_1` FOREIGN KEY (`search_id`) REFERENCES `collection_sources` (`id`),
  CONSTRAINT `jobs_ibfk_2` FOREIGN KEY (`download_post_id`) REFERENCES `posts` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `library_folders` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `query_key` varchar(512) COLLATE utf8mb4_0900_bin NOT NULL,
  `folder_name` varchar(191) COLLATE utf8mb4_0900_bin NOT NULL,
  `is_canonical` tinyint(1) NOT NULL DEFAULT '0',
  `canonical_query` varchar(512) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `folder_name` (`folder_name`),
  UNIQUE KEY `canonical_query` (`canonical_query`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `media_files` (
  `media_id` bigint NOT NULL,
  `file_id` bigint NOT NULL,
  PRIMARY KEY (`media_id`,`file_id`),
  KEY `media_files_file` (`file_id`),
  CONSTRAINT `media_files_ibfk_1` FOREIGN KEY (`media_id`) REFERENCES `post_media` (`id`),
  CONSTRAINT `media_files_ibfk_2` FOREIGN KEY (`file_id`) REFERENCES `files` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `post_media` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `post_id` bigint NOT NULL,
  `media_role` enum('original','preview','sample','attachment') COLLATE utf8mb4_0900_bin NOT NULL,
  `position` int NOT NULL DEFAULT '0',
  `url` text COLLATE utf8mb4_0900_bin,
  `mime_type` varchar(128) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  `extension` varchar(32) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  `width` bigint DEFAULT NULL,
  `height` bigint DEFAULT NULL,
  `file_size_bytes` bigint DEFAULT NULL,
  `duration_ms` bigint DEFAULT NULL,
  `remote_md5` varchar(128) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  `checksum_algorithm` varchar(16) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  `checksum` varchar(256) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `post_role_position` (`post_id`,`media_role`,`position`),
  KEY `idx_gallery_remote_md5` (`remote_md5`),
  CONSTRAINT `post_media_ibfk_1` FOREIGN KEY (`post_id`) REFERENCES `posts` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `post_relations` (
  `post_id` bigint NOT NULL,
  `related_source_id` bigint NOT NULL,
  `related_remote_id` varchar(128) COLLATE utf8mb4_0900_bin NOT NULL,
  `relation_type` varchar(40) COLLATE utf8mb4_0900_bin NOT NULL,
  PRIMARY KEY (`post_id`,`related_source_id`,`related_remote_id`,`relation_type`),
  KEY `related_source_id` (`related_source_id`),
  CONSTRAINT `post_relations_ibfk_1` FOREIGN KEY (`post_id`) REFERENCES `posts` (`id`) ON DELETE CASCADE,
  CONSTRAINT `post_relations_ibfk_2` FOREIGN KEY (`related_source_id`) REFERENCES `sources` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `post_source_links` (
  `post_id` bigint NOT NULL,
  `position` int NOT NULL,
  `url` text COLLATE utf8mb4_0900_bin NOT NULL,
  PRIMARY KEY (`post_id`,`position`),
  CONSTRAINT `post_source_links_ibfk_1` FOREIGN KEY (`post_id`) REFERENCES `posts` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `post_tag_text` (
  `post_id` bigint NOT NULL,
  `tags` mediumtext COLLATE utf8mb4_0900_bin NOT NULL,
  PRIMARY KEY (`post_id`),
  CONSTRAINT `post_tag_text_ibfk_1` FOREIGN KEY (`post_id`) REFERENCES `posts` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `post_tags` (
  `post_id` bigint NOT NULL,
  `tag_id` bigint NOT NULL,
  PRIMARY KEY (`post_id`,`tag_id`),
  KEY `post_tags_tag` (`tag_id`,`post_id`),
  CONSTRAINT `post_tags_ibfk_1` FOREIGN KEY (`post_id`) REFERENCES `posts` (`id`) ON DELETE CASCADE,
  CONSTRAINT `post_tags_ibfk_2` FOREIGN KEY (`tag_id`) REFERENCES `tags` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `posts` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `source_id` bigint NOT NULL,
  `remote_id` varchar(128) COLLATE utf8mb4_0900_bin NOT NULL,
  `sort_key` double NOT NULL DEFAULT '0',
  `post_url` text COLLATE utf8mb4_0900_bin,
  `rating` varchar(32) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  `score` double DEFAULT NULL,
  `favorite_count` bigint DEFAULT NULL,
  `title` text COLLATE utf8mb4_0900_bin,
  `description` mediumtext COLLATE utf8mb4_0900_bin,
  `tags_text` mediumtext COLLATE utf8mb4_0900_bin NOT NULL,
  `source_text` text COLLATE utf8mb4_0900_bin,
  `metadata_note` text COLLATE utf8mb4_0900_bin,
  `remote_created_at` datetime(6) DEFAULT NULL,
  `remote_updated_at` datetime(6) DEFAULT NULL,
  `first_seen_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `last_seen_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `availability_status` varchar(40) COLLATE utf8mb4_0900_bin NOT NULL DEFAULT 'available',
  PRIMARY KEY (`id`),
  UNIQUE KEY `posts_site_id` (`source_id`,`remote_id`),
  KEY `posts_sort_cover` (`id`,`sort_key`),
  CONSTRAINT `posts_ibfk_1` FOREIGN KEY (`source_id`) REFERENCES `sources` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `result_order` (
  `search_id` bigint NOT NULL,
  `post_id` bigint NOT NULL,
  `added_key` bigint NOT NULL,
  `post_sort` double NOT NULL,
  `score` double NOT NULL,
  `pixels` bigint NOT NULL,
  PRIMARY KEY (`search_id`,`post_id`),
  KEY `result_order_added_key` (`search_id`,`added_key`,`post_id`),
  KEY `result_order_post_sort` (`search_id`,`post_sort`,`post_id`,`added_key`),
  KEY `result_order_score` (`search_id`,`score`,`post_id`,`added_key`),
  KEY `result_order_pixels` (`search_id`,`pixels`,`post_id`,`added_key`),
  KEY `result_order_post_id` (`post_id`),
  CONSTRAINT `result_order_ibfk_1` FOREIGN KEY (`search_id`) REFERENCES `collection_sources` (`id`) ON DELETE CASCADE,
  CONSTRAINT `result_order_ibfk_2` FOREIGN KEY (`post_id`) REFERENCES `posts` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `schema_migrations` (
  `version` varchar(100) COLLATE utf8mb4_0900_bin NOT NULL,
  `applied_at` datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`version`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `search_counts` (
  `search_id` bigint NOT NULL,
  `total` bigint NOT NULL,
  PRIMARY KEY (`search_id`),
  CONSTRAINT `search_counts_ibfk_1` FOREIGN KEY (`search_id`) REFERENCES `collection_sources` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `settings` (
  `key` varchar(191) COLLATE utf8mb4_0900_bin NOT NULL,
  `value` text COLLATE utf8mb4_0900_bin NOT NULL,
  PRIMARY KEY (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `source_allowed_hosts` (
  `source_id` bigint NOT NULL,
  `hostname` varchar(253) COLLATE utf8mb4_0900_bin NOT NULL,
  PRIMARY KEY (`source_id`,`hostname`),
  CONSTRAINT `source_allowed_hosts_ibfk_1` FOREIGN KEY (`source_id`) REFERENCES `sources` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `source_checks` (
  `source_id` bigint NOT NULL,
  `ok` tinyint(1) NOT NULL,
  `message` text COLLATE utf8mb4_0900_bin NOT NULL,
  `checked_at` datetime(6) NOT NULL,
  `nsfw_available` tinyint(1) DEFAULT NULL,
  PRIMARY KEY (`source_id`),
  CONSTRAINT `source_checks_ibfk_1` FOREIGN KEY (`source_id`) REFERENCES `sources` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `source_field_mappings` (
  `source_id` bigint NOT NULL,
  `field_name` varchar(100) COLLATE utf8mb4_0900_bin NOT NULL,
  `field_path` text COLLATE utf8mb4_0900_bin NOT NULL,
  PRIMARY KEY (`source_id`,`field_name`),
  CONSTRAINT `source_field_mappings_ibfk_1` FOREIGN KEY (`source_id`) REFERENCES `sources` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `source_options` (
  `source_id` bigint NOT NULL,
  `option_name` varchar(100) COLLATE utf8mb4_0900_bin NOT NULL,
  `value_type` enum('str','int','float','bool','null') COLLATE utf8mb4_0900_bin NOT NULL,
  `option_value` text COLLATE utf8mb4_0900_bin,
  PRIMARY KEY (`source_id`,`option_name`),
  CONSTRAINT `source_options_ibfk_1` FOREIGN KEY (`source_id`) REFERENCES `sources` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `source_runtime` (
  `source_id` bigint NOT NULL,
  `backoff_until` datetime(6) DEFAULT NULL,
  `consecutive_failures` bigint NOT NULL DEFAULT '0',
  `last_download_started_at` datetime(6) DEFAULT NULL,
  PRIMARY KEY (`source_id`),
  CONSTRAINT `source_runtime_ibfk_1` FOREIGN KEY (`source_id`) REFERENCES `sources` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `source_tags` (
  `source_id` bigint NOT NULL,
  `tag_id` bigint NOT NULL,
  `remote_tag_id` varchar(128) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  `category` varchar(40) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  PRIMARY KEY (`source_id`,`tag_id`),
  KEY `tag_id` (`tag_id`),
  CONSTRAINT `source_tags_ibfk_1` FOREIGN KEY (`source_id`) REFERENCES `sources` (`id`),
  CONSTRAINT `source_tags_ibfk_2` FOREIGN KEY (`tag_id`) REFERENCES `tags` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `sources` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `name` varchar(191) COLLATE utf8mb4_0900_bin NOT NULL,
  `url` varchar(512) COLLATE utf8mb4_0900_bin NOT NULL,
  `api` varchar(40) COLLATE utf8mb4_0900_bin NOT NULL,
  `enabled` tinyint(1) NOT NULL DEFAULT '1',
  `deleted_at` datetime(6) DEFAULT NULL,
  `api_url` varchar(512) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  `api_path` varchar(512) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  `post_path` varchar(512) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  `download_subdir` varchar(512) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  `user_agent` text COLLATE utf8mb4_0900_bin,
  `min_delay` double DEFAULT NULL,
  `download_interval` double DEFAULT NULL,
  `requires_credentials` tinyint(1) DEFAULT NULL,
  `auth_mode` varchar(32) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  `api_key_name` varchar(80) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  `response_path` varchar(512) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  `query_param` varchar(100) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  `page_param` varchar(100) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  `limit_param` varchar(100) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  `page_base` int DEFAULT NULL,
  `notes` text COLLATE utf8mb4_0900_bin,
  `credential_ref` varchar(100) COLLATE utf8mb4_0900_bin DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `sources_url_api` (`url`,`api`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `storage_roots` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `name` varchar(191) COLLATE utf8mb4_0900_bin NOT NULL,
  `configured_path` varchar(512) COLLATE utf8mb4_0900_bin NOT NULL,
  `enabled` tinyint(1) NOT NULL DEFAULT '1',
  PRIMARY KEY (`id`),
  UNIQUE KEY `configured_path` (`configured_path`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `tag_aliases` (
  `source_id` bigint NOT NULL,
  `alias_name` varchar(512) COLLATE utf8mb4_0900_bin NOT NULL,
  `canonical_tag_id` bigint NOT NULL,
  PRIMARY KEY (`source_id`,`alias_name`),
  KEY `canonical_tag_id` (`canonical_tag_id`),
  CONSTRAINT `tag_aliases_ibfk_1` FOREIGN KEY (`source_id`) REFERENCES `sources` (`id`),
  CONSTRAINT `tag_aliases_ibfk_2` FOREIGN KEY (`canonical_tag_id`) REFERENCES `tags` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `tag_grams` (
  `gram` varchar(3) COLLATE utf8mb4_0900_bin NOT NULL,
  `tag_id` bigint NOT NULL,
  PRIMARY KEY (`gram`,`tag_id`),
  KEY `tag_id` (`tag_id`),
  CONSTRAINT `tag_grams_ibfk_1` FOREIGN KEY (`tag_id`) REFERENCES `tags` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `tag_index_dirty` (
  `post_id` bigint NOT NULL,
  `revision` bigint NOT NULL DEFAULT '1',
  PRIMARY KEY (`post_id`),
  CONSTRAINT `tag_index_dirty_ibfk_1` FOREIGN KEY (`post_id`) REFERENCES `posts` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `tags` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `signature` char(64) COLLATE utf8mb4_0900_bin NOT NULL,
  `name` text COLLATE utf8mb4_0900_bin NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `signature` (`signature`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;


-- Marks a newly initialized relational database as ready for this app.
INSERT IGNORE INTO schema_migrations(version) VALUES ('relational_v2');
