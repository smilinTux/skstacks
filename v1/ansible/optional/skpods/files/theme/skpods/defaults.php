<?php
/**
 * skpods.id theme for the skpods Nextcloud engine.
 *
 * Replaces every product string the engine shows (page titles, the login
 * headline, footer, help and client links) with skpods.id. The theming app
 * settings the role writes (name, url, slogan, colors) take precedence where
 * both exist; this file covers the strings the theming app does not.
 */
class OC_Theme {
	private const NAME = 'skpods.id';
	private const URL = 'https://skpods.id';

	public function getBaseUrl(): string {
		return self::URL;
	}

	public function getDocBaseUrl(): string {
		return self::URL . '/docs';
	}

	public function getTitle(): string {
		return self::NAME;
	}

	public function getName(): string {
		return self::NAME;
	}

	public function getHTMLName(): string {
		return self::NAME;
	}

	public function getEntity(): string {
		return self::NAME;
	}

	public function getProductName(): string {
		return self::NAME;
	}

	public function getSlogan(?string $lang = null): string {
		return 'your data, your pod, your rules';
	}

	public function getShortFooter(): string {
		return '<a href="' . self::URL . '" target="_blank" rel="noreferrer noopener" class="entity-name">' . self::NAME . '</a>'
			. ' &middot; sovereign pods for humans and their AIs';
	}

	public function getLongFooter(): string {
		return $this->getShortFooter();
	}

	public function buildDocLinkToKey($key): string {
		return $this->getDocBaseUrl();
	}

	public function getiTunesAppId(): string {
		return '';
	}

	public function getiOSClientUrl(): string {
		return self::URL;
	}

	public function getAndroidClientUrl(): string {
		return self::URL;
	}

	public function getFDroidClientUrl(): string {
		return self::URL;
	}

	public function getSyncClientUrl(): string {
		return self::URL;
	}

	public function getColorPrimary(): string {
		return '#7c3aed';
	}

	public function getColorBackground(): string {
		return '#05060d';
	}

	public function getScssVariables(): array {
		return [];
	}
}
